"""Exercise real child interpreters and compare all serial/parallel output."""

import _support
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from sql_partition_analysis.stages.parallel_analyze import automatic_workers, cpu_quota



class ParallelAnalysisTests(unittest.TestCase):
    def test_automatic_worker_count_respects_available_cpus_and_fallback(self):
        module = 'sql_partition_analysis.partitioning.parallel.os'
        with patch('sql_partition_analysis.partitioning.parallel.cpu_quota', return_value=None):
            with patch(module + '.sched_getaffinity', return_value={0, 1, 2}, create=True):
                self.assertEqual(automatic_workers(), 3)
            for cpus, expected in ((12, 12), (128, 32), (None, 1)):
                with patch(module + '.sched_getaffinity', side_effect=OSError, create=True), patch(module + '.cpu_count', return_value=cpus):
                    self.assertEqual(automatic_workers(), expected)
        with patch(module + '.sched_getaffinity', return_value=set(range(8)), create=True), patch('sql_partition_analysis.partitioning.parallel.cpu_quota', return_value=2):
            self.assertEqual(automatic_workers(), 2)

    def test_cgroup_v1_v2_quota_and_unlimited(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertIsNone(cpu_quota(root))
            (root / 'cpu.max').write_text('150000 100000')
            self.assertEqual(cpu_quota(root), 2)
            (root / 'cpu.max').write_text('max 100000')
            self.assertIsNone(cpu_quota(root))
            (root / 'cpu').mkdir()
            (root / 'cpu/cpu.cfs_quota_us').write_text('300000')
            (root / 'cpu/cpu.cfs_period_us').write_text('100000')
            self.assertEqual(cpu_quota(root), 3)
            (root / 'cpu/cpu.cfs_quota_us').write_text('-1')
            self.assertIsNone(cpu_quota(root))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='parallel-sql-test-')
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        config = {'version': 1, 'tables': {
            's.events': {'partition_columns': ['data_dt']},
            's.target': {'partition_columns': ['data_dt']},
        }, 'non_partitioned_tables': ['s.plain']}
        self.config = self.folder / 'partition_config.json'
        self.config.write_text(json.dumps(config), encoding='utf-8')
        read = "SELECT * FROM s.events WHERE data_dt IN ('2026-09-01', '2026-09-02')"
        statements = [
            (read, 'alice', 7, ['SELECT', 'FETCH']),
            (read, 'bob', 3, ['FETCH']),
            ("WITH c AS (SELECT data_dt FROM s.events) SELECT * FROM c WHERE data_dt = '2026-09-03'", 'alice', 2, ['SELECT']),
            ("INSERT INTO s.target(data_dt) SELECT DATE '2026-09-01' UNION ALL SELECT DATE '2026-09-02'", 'bob', 4, ['INSERT']),
            ("SELECT * FROM s.events a JOIN s.plain b ON a.id=b.id WHERE data_dt = '2026-09-04'", 'alice', 9, ['SELECT']),
            ("ALTER TABLE s.events TRUNCATE PARTITION FOR (DATE '2026-09-05')", 'bob', 1, ['ALTER']),
            ("SELECT * FROM s.events a JOIN s.events b ON a.data_dt=b.data_dt WHERE a.data_dt='2026-09-06'", 'alice', 6, ['FETCH']),
            ('not a SQL message', 'bob', 11, ['']),
            ('SELECT ( FROM', 'alice', 2, ['SELECT']),
        ]
        records = [{'id': f'SQL{i:06d}', 'sql': sql, 'user_name': user,
                    'count': count, 'types': kinds, 'first_record': i,
                    'first_time': f'2026-10-01 00:00:{i:02d}',
                    'last_time': f'2026-10-01 00:01:{i:02d}'}
                   for i, (sql, user, count, kinds) in enumerate(statements, 1)]
        self.profile = {'sqls': records, 'widths': {'33': sum(r['count'] for r in records)}}
        (self.folder / 'profile.json').write_text(json.dumps(self.profile), encoding='utf-8')
        self.env = _support.subprocess_env( SQL_ANALYSIS_OUTPUT_DIR=str(self.folder),
                        SQL_ANALYSIS_PARTITION_CONFIG=str(self.config))

    def invoke(self, script, *args):
        return subprocess.run(_support.stage_command(script) + list(args),
                              env=self.env, capture_output=True, text=True, timeout=60)

    def read_output(self):
        return json.loads((self.folder / 'analysis.json').read_text(encoding='utf-8'))

    def test_parallel_and_single_worker_equal_serial_including_counts_and_order(self):
        serial = self.invoke('analyze.py')
        self.assertEqual(serial.returncode, 0, serial.stderr)
        expected = self.read_output()
        self.assertTrue(expected['unresolved'])
        self.assertEqual(expected['metrics']['raw_distinct_messages'], 8)
        self.assertEqual(expected['metrics']['sql_user_contexts'], 9)
        for workers in (1, 3, 32):
            with self.subTest(workers=workers):
                parallel = self.invoke('parallel_analyze.py', '--workers', str(workers))
                self.assertEqual(parallel.returncode, 0, parallel.stderr)
                self.assertEqual(self.read_output(), expected)
                for chunk in (self.folder / '.analysis_workers').glob('run-*/worker-*/profile.json'):
                    profile = json.loads(chunk.read_text())
                    self.assertEqual(sum(profile['widths'].values()), sum(r['count'] for r in profile['sqls']))

    def test_worker_failure_does_not_publish_partial_result(self):
        previous = b'{"previous_successful_result": true}'
        (self.folder / 'analysis.json').write_bytes(previous)
        self.config.write_text('{invalid json', encoding='utf-8')
        result = self.invoke('parallel_analyze.py', '--workers', '3')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('worker.log', result.stderr)
        self.assertEqual((self.folder / 'analysis.json').read_bytes(), previous)

if __name__ == '__main__':
    unittest.main()
