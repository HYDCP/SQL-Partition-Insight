"""Structural contracts that keep modules reusable and runs isolated."""
import _support
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from sql_partition_analysis.partitioning.analyzer import analyze_profile
from sql_partition_analysis.partitioning.catalog import PartitionCatalog
from sql_partition_analysis.analytics.aggregation import aggregate_accesses
from sql_partition_analysis.ingestion.profile import profile_logs
from sql_partition_analysis.reporting.export import export_reports
import csv
import gzip

class ProjectStructureTests(unittest.TestCase):
    def test_reusable_services_isolate_catalogs_and_preserve_observations(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source.csv'
            sqls = ["SELECT * FROM s.events WHERE data_dt IN ('2026-09-01','2026-09-02') AND row_date='2026-08-01'",
                    "INSERT INTO s.events(data_dt) VALUES (DATE '2026-09-01')",
                    "SELECT * FROM s.events"]
            with source.open('w', newline='') as stream:
                writer = csv.writer(stream)
                writer.writerow(['column%d' % i for i in range(1, 34)])
                for i, statement in enumerate([sqls[0], sqls[0], sqls[1], sqls[2]]):
                    row = [''] * 33
                    row[0], row[2], row[21] = '2026-10-01 00:00:%02d' % i, 's', 'statement: ' + statement
                    writer.writerow(row)
            profile = profile_logs(source)
            original = copy.deepcopy(profile)
            catalogs = [PartitionCatalog({'version': 1, 'tables': {'s.events': {'partition_columns': [field]}}})
                        for field in ('data_dt', 'row_date')]
            with patch.dict(os.environ, {'SQL_ANALYSIS_PARTITION_CONFIG': '/missing', 'SQL_ANALYSIS_OUTPUT_DIR': '/missing'}):
                analysis = analyze_profile(profile, catalogs[0])
                result = aggregate_accesses(profile, analysis, catalogs[0])
                other = aggregate_accesses(profile, analyze_profile(profile, catalogs[1]), catalogs[1])
            self.assertEqual(profile, original)
            self.assertIn('row_date=2026-08-01', [r['partition'] for r in other['results']])
            self.assertNotIn('row_date=2026-08-01', [r['partition'] for r in result['results']])
            before = copy.deepcopy((analysis, result))
            targets = []
            for name in ('first', 'second'):
                folder = root / name
                folder.mkdir()
                export_reports(profile, analysis, result, source, folder)
                self.assertEqual((analysis, result), before)
                targets.append([json.loads(line) for line in (folder / 'access_targets.jsonl').read_text().splitlines()])
            self.assertEqual(targets[0], targets[1])
            by_id = {r['access_id']: r for r in targets[0]}
            with gzip.open(root / 'first/access_observations.jsonl.gz', 'rt') as stream:
                events = [json.loads(line) for line in stream]
            self.assertEqual([e['source_record'] for e in events], [1, 2, 3, 4])
            self.assertEqual(events[0]['access_ids'], events[1]['access_ids'])
            self.assertEqual(len(events[0]['access_ids']), 2)
            self.assertEqual({by_id[key]['operation'] for key in events[2]['access_ids']}, {'insert'})
            self.assertTrue(all('分区未知' in by_id[key]['partition_condition'] for key in events[3]['access_ids']))

    def test_check_rejects_bad_partition_contents_and_missing_output_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / 'config.json'
            partition = root / 'partitions.json'
            valid = {'version': 1, 'tables': {'s.t': {'partition_columns': ['data_dt']}}}
            config = {'version': 1, 'source': {'type': 'mysql', 'mysql': {
                'host': '127.0.0.1', 'user': 'test', 'database': 'logs', 'table': ['queries']}},
                'partition': {'format': 'config_json', 'path': 'partitions.json'},
                'output': {'directory': 'runs'}}
            for case in ('invalid_partition', 'missing_output', 'file_as_output', 'invalid_metadata_csv'):
                with self.subTest(case=case):
                    current = json.loads(json.dumps(config))
                    partition.write_text(json.dumps(valid))
                    if case == 'invalid_partition':
                        partition.write_text(json.dumps({'version': 1, 'tables': {'s.t': {'partition_columns': []}}}))
                    elif case == 'missing_output':
                        current['output'] = {}
                    elif case == 'file_as_output':
                        current['output']['directory'] = 'partitions.json'
                    else:
                        current['partition']['format'] = 'metadata_csv'
                        partition.write_text('wrong,columns\na,b\n')
                    path.write_text(json.dumps(current))
                    result = subprocess.run([sys.executable, str(_support.CLI), '--config', str(path), '--check'],
                                            cwd=tmp, capture_output=True, text=True, timeout=20)
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertFalse((root / 'runs').exists())

    def test_importing_modules_requires_no_inputs_and_creates_no_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = _support.subprocess_env()
            for name in list(env):
                if name.startswith('SQL_ANALYSIS_'):
                    del env[name]
            code = (
                'import importlib, pkgutil, sql_partition_analysis\n'
                'for info in pkgutil.walk_packages(sql_partition_analysis.__path__, '
                'sql_partition_analysis.__name__ + "."):\n'
                '    if not info.name.endswith(".__main__"):\n'
                '        importlib.import_module(info.name)\n'
            )
            result = subprocess.run([sys.executable, '-c', code], cwd=tmp, env=env,
                                    capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, '')
            self.assertEqual(list(Path(tmp).iterdir()), [])

if __name__ == '__main__':
    unittest.main()
