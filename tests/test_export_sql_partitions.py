"""Default recognition output preserves all SQL evidence and stays outside ZIP."""
import _support
import contextlib
import copy
import csv
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from sql_partition_analysis.analytics.aggregation import aggregate_accesses
from sql_partition_analysis.ingestion.profile import profile_logs
from sql_partition_analysis.ingestion.records import COLUMNS
from sql_partition_analysis.mysql_pipeline import run_mysql_analysis
from sql_partition_analysis.partitioning.analyzer import analyze_profile
from sql_partition_analysis.partitioning.catalog import PartitionCatalog
from sql_partition_analysis.pipeline import run
from sql_partition_analysis.reporting.export import export_reports
from sql_partition_analysis.reporting.sql_partitions import OUTPUT_NAME, export_sql_partitions
from test_mysql_streaming import BatchConnection, BatchCursor


class SQLPartitionExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='sql-partition-export-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        source = self.root / 'source.csv'
        read = "SELECT * FROM s.events WHERE data_dt IN ('2026-09-01','2026-09-02')"
        statements = [(read, 'alice'), (read, 'alice'), (read, 'bob'),
                      ('SELECT * FROM s.plain', 'alice'), ('SELECT * FROM events', 'alice'),
                      ('SELECT * FROM s.other', 'alice'), ('connection authorized', 'alice'),
                      ('SELECT ( FROM', 'alice'),
                      ("SELECT * FROM s.events a JOIN s.events b ON a.id=b.id "
                       "WHERE a.data_dt='2026-09-03' AND b.data_dt='2026-09-04'", 'alice'),
                      ("SELECT * FROM s.events a JOIN s.target b ON a.id=b.id "
                       "WHERE data_dt='2026-09-03'", 'alice'),
                      ('COPY s.events (data_dt,', 'alice'), ('SELECT 1\x18', 'alice')]
        self.rows = []
        for number, (sql, user) in enumerate(statements):
            row = [''] * 33
            row[0] = '2026-10-01 00:00:{:02d} CST'.format(number)
            row[2], row[18], row[21] = user, 'SELECT', 'statement: ' + sql
            self.rows.append(row)
        with source.open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(COLUMNS)
            writer.writerows(self.rows)
        catalog = PartitionCatalog({'version': 1, 'tables': {
            's.events': {'partition_columns': ['data_dt'], 'partition_type': 'RANGE'},
            's.target': {'partition_columns': ['data_dt']}}, 'non_partitioned_tables': ['s.plain']})
        self.catalog_config = catalog.config
        self.profile = profile_logs(source)
        self.analysis = analyze_profile(self.profile, catalog)
        aggregate = aggregate_accesses(self.profile, self.analysis, catalog)
        export_reports(self.profile, self.analysis, aggregate, source, self.root)
        self.save('profile.json', self.profile)
        self.save('analysis.json', self.analysis)
        self.save('run_summary.json', {'status': 'complete', 'source_rows': len(statements)})
        (self.root / '统计结果.zip').write_bytes(b'preserved result bundle')
        source.unlink()

    def save(self, name, value):
        (self.root / name).write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')

    def export(self, profile=None):
        report = json.loads((self.root / 'report_data.json').read_text())
        return export_sql_partitions(profile or self.profile, self.analysis, report, self.root)

    def read_export(self, root=None):
        return [json.loads(line) for line in ((root or self.root) / OUTPUT_NAME).read_text(encoding='utf-8').splitlines()]

    def config_path(self, source_type):
        self.save('partition_config.json', self.catalog_config)
        config = {'version': 1, 'source': {'type': source_type},
                  'partition': {'format': 'config_json', 'path': 'partition_config.json'},
                  'output': {'directory': 'runs', 'xlsx': False}}
        if source_type == 'csv':
            with (self.root / 'source.csv').open('w', encoding='utf-8-sig', newline='') as stream:
                writer = csv.writer(stream)
                writer.writerow(COLUMNS)
                writer.writerows(self.rows)
            config['source']['csv'] = {'path': 'source.csv'}
        else:
            config['source']['mysql'] = {'host': '127.0.0.1', 'user': 'test', 'database': 'logs',
                                         'table': ['day1'], 'batch_rows': 2, 'password': ''}
        self.save('config.json', config)
        return self.root / 'config.json'

    def run_mysql(self):
        path = self.config_path('mysql')
        connection = BatchConnection(BatchCursor({'day1': self.rows}))
        def fake_connection_run(*args, **kwargs):
            return run_mysql_analysis(*args, connect=lambda **kw: connection, password='', workers=1, **kwargs)
        with patch('sql_partition_analysis.pipeline.run_mysql_analysis', side_effect=fake_connection_run), \
                contextlib.redirect_stdout(io.StringIO()):
            output = run(path)
        self.assertTrue(connection.closed)
        return output

    def assert_default_output(self, output):
        rows = self.read_export(output)
        self.assertEqual(len(rows), 11)
        self.assertEqual(sum(row['log_count'] for row in rows), 12)
        summary = json.loads((output / 'run_summary.json').read_text())
        self.assertEqual(summary['status'], 'complete')
        with zipfile.ZipFile(output / '统计结果.zip') as archive:
            self.assertNotIn(OUTPUT_NAME, archive.namelist())
            self.assertIsNone(archive.testzip())
        return rows

    def test_default_service_export_preserves_all_contexts_and_original_run_files(self):
        # export_reports already produced the JSONL by default, before source removal.
        self.assertTrue((self.root / OUTPUT_NAME).is_file())
        before = {path.name: path.read_bytes() for path in self.root.iterdir() if path.is_file()}
        destination = self.export()
        self.assertEqual(destination, self.root / OUTPUT_NAME)
        rows = self.read_export()
        self.assertEqual([row['sql_id'] for row in rows], [row['id'] for row in self.profile['sqls']])
        self.assertEqual(sum(row['log_count'] for row in rows), 12)
        self.assertEqual(len(rows), 11)
        for row, record in zip(rows, self.profile['sqls']):
            self.assertEqual(row['sql'], record['sql'])
            self.assertEqual(row['user_name'], record['user_name'])
            self.assertEqual(row['log_count'], record['count'])
        repeated = rows[0]
        self.assertEqual(repeated['log_count'], 2)
        self.assertTrue(repeated['included_in_statistics'])
        parts = repeated['tables'][0]['recognized_partitions']
        self.assertEqual({part['raw_partition'] for part in parts}, {'data_dt=2026-09-01', 'data_dt=2026-09-02'})
        self.assertTrue(all(part['basis'] for part in parts))
        self.assertTrue(repeated['tables'][0]['predicates'])
        excluded = {row['tables'][0]['table']: row['tables'][0]['exclusion_reason']
                    for row in rows if row['tables'] and not row['included_in_statistics']}
        self.assertEqual(excluded['s.plain'], '清单标明非分区表')
        self.assertEqual(excluded['[未指定schema].events'], '未指定 schema，不自动补全')
        self.assertEqual(excluded['s.other'], '不在明确分区表清单内')
        self.assertIn('非SQL日志消息', {row['parse_status'] for row in rows})
        self.assertIn('解析失败', {row['parse_status'] for row in rows})
        self.assertIn('部分解析（COPY截断）', {row['parse_status'] for row in rows})
        self.assertTrue(any(row['unresolved'] for row in rows))
        joined = next(row for row in rows if 'a.data_dt=' in row['sql'])
        self.assertEqual(len(joined['tables']), 2)
        self.assertEqual([item['occurrence'] for item in joined['tables']], [1, 2])
        self.assertEqual([item['recognized_partitions'][0]['raw_partition'] for item in joined['tables']],
                         ['data_dt=2026-09-03', 'data_dt=2026-09-04'])
        for name, data in before.items():
            self.assertEqual((self.root / name).read_bytes(), data, name)

    def test_default_csv_command_generates_jsonl_outside_zip(self):
        path = self.config_path('csv')
        result = subprocess.run([sys.executable, str(_support.CLI), '--config', str(path)],
                                cwd='/', env=_support.subprocess_env(), capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        output, = (self.root / 'runs').iterdir()
        self.assert_default_output(output)

    def test_default_mysql_pipeline_generates_jsonl_outside_zip_without_raw_csv(self):
        output = self.run_mysql()
        self.assert_default_output(output)
        self.assertFalse((output / '源日志.csv').exists())
        self.assertFalse((output / '.mysql_observations.jsonl.gz').exists())

    def test_failed_access_to_selected_table_is_excluded_from_every_statistical_report(self):
        statement = "SELECT * FROM s.events WHERE data_dt=DATE '20261007'"
        self.rows = []
        for number, sql in enumerate((statement,
                'An exception was encountered during the execution of statement: ' + statement)):
            row = [''] * 33
            row[0] = '2026-10-01 00:00:{:02d} CST'.format(number)
            row[2], row[18], row[21] = 'alice', 'SELECT', 'statement: ' + sql
            self.rows.append(row)
        for source in ('csv', 'mysql'):
            with self.subTest(source=source), contextlib.redirect_stdout(io.StringIO()):
                output = run(self.config_path('csv')) if source == 'csv' else self.run_mysql()
                recognition = self.read_export(output)
                successful, failed = recognition
                sid = successful['sql_id']
                self.assertTrue(successful['included_in_statistics'])
                self.assertFalse(failed['included_in_statistics'])
                self.assertEqual(failed['tables'][0]['table'], 's.events')
                self.assertIn('异常执行日志', failed['tables'][0]['exclusion_reason'])
                self.assertEqual(failed['sql'], self.rows[1][21][len('statement: '):])
                for name in ('SQL解析明细.csv', 'SQL原文索引.csv'):
                    with (output / name).open(encoding='utf-8-sig', newline='') as stream:
                        rows = list(csv.DictReader(stream))
                    self.assertEqual([item['SQL编号'] for item in rows], [sid], name)
                with (output / '表分区配置核对.csv').open(encoding='utf-8-sig', newline='') as stream:
                    coverage = list(csv.DictReader(stream))
                self.assertEqual(len(coverage), 1)
                self.assertEqual(coverage[0]['访问日志条数_同条日志同表计一次'], '1')
                self.assertEqual(coverage[0]['关联SQL编号'], sid)
                report = json.loads((output / 'report_data.json').read_text(encoding='utf-8'))
                self.assertEqual(report['metrics']['access_log_count'], 1)
                self.assertEqual(report['metrics']['source_rows'], 2)
                self.assertEqual({item['id'] for item in report['mapping']}, {sid})
                manifest = json.loads((output / 'access_manifest.json').read_text(encoding='utf-8'))
                self.assertEqual(manifest['included_records'], 1)

    def test_inconsistent_results_do_not_replace_previous_export(self):
        destination = self.root / OUTPUT_NAME
        destination.write_text('previous successful export', encoding='utf-8')
        bad = copy.deepcopy(self.profile)
        bad['widths'] = {'33': 999}
        with self.assertRaisesRegex(ValueError, '不一致'):
            self.export(bad)
        bad = copy.deepcopy(self.profile)
        bad['sqls'][0]['id'] = 'unknown'
        with self.assertRaisesRegex(ValueError, '不一致'):
            self.export(bad)
        self.assertEqual(destination.read_text(), 'previous successful export')
        self.assertFalse(list(self.root.glob('*.part')))

    def test_write_failure_is_atomic_and_cleans_pending_export(self):
        destination = self.root / OUTPUT_NAME
        destination.write_text('previous successful export', encoding='utf-8')
        original = json.dumps
        calls = []
        def fail_after_one_record(*args, **kwargs):
            calls.append(True)
            if len(calls) == 2:
                raise OSError('simulated disk write failure')
            return original(*args, **kwargs)
        with patch('sql_partition_analysis.reporting.sql_partitions.json.dumps', side_effect=fail_after_one_record):
            with self.assertRaisesRegex(OSError, 'simulated'):
                self.export()
        self.assertEqual(destination.read_text(), 'previous successful export')
        self.assertFalse(list(self.root.glob('*.part')))

    def test_unknown_partition_mapping_is_rejected_before_writing(self):
        report = json.loads((self.root / 'report_data.json').read_text())
        report['mapping'][0]['occurrence'] = 999
        self.save('report_data.json', report)
        previous = (self.root / OUTPUT_NAME).read_bytes()
        with self.assertRaisesRegex(ValueError, '未知表引用'):
            self.export()
        self.assertEqual((self.root / OUTPUT_NAME).read_bytes(), previous)

    def test_default_jsonl_failure_prevents_completed_bundle_and_cleans_mysql_journal(self):
        path = self.config_path('mysql')
        connection = BatchConnection(BatchCursor({'day1': self.rows}))
        def fake_connection_run(*args, **kwargs):
            return run_mysql_analysis(*args, connect=lambda **kw: connection, password='', workers=1, **kwargs)
        with patch('sql_partition_analysis.pipeline.run_mysql_analysis', side_effect=fake_connection_run), \
                patch('sql_partition_analysis.reporting.sql_partitions.export_sql_partitions', side_effect=OSError('JSONL export failed')), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(OSError, 'JSONL export failed'):
                run(path)
        output, = (self.root / 'runs').iterdir()
        self.assertEqual(json.loads((output / 'run_summary.json').read_text())['status'], 'failed')
        for name in ('统计结果.zip', 'access_manifest.json', '.mysql_observations.jsonl.gz', OUTPUT_NAME):
            self.assertFalse((output / name).exists(), name)
        self.assertTrue(connection.closed)

    def test_check_only_does_not_start_export(self):
        path = self.config_path('mysql')
        with patch('sql_partition_analysis.reporting.sql_partitions.export_sql_partitions', side_effect=AssertionError('export must not run')), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(run(path, check_only=True))
        self.assertFalse((self.root / 'runs').exists())


if __name__ == '__main__':
    unittest.main()
