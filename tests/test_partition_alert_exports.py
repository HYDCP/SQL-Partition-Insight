"""Independent diagnostics are published for both transports, without stopping a run."""
import _support
import contextlib
import copy
import csv
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from sql_partition_analysis.analytics.aggregation import aggregate_accesses
from sql_partition_analysis.ingestion.records import COLUMNS
from sql_partition_analysis.mysql_pipeline import run_mysql_analysis
from sql_partition_analysis.partitioning.analyzer import analyze_profile
from sql_partition_analysis.partitioning.catalog import PartitionCatalog
from sql_partition_analysis.pipeline import run
from sql_partition_analysis.reporting.partition_alerts import ALERTS_NAME, CHECK_NAME
from test_mysql_streaming import BatchConnection, BatchCursor


class PartitionAlertExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='partition-alert-export-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.catalog = {'version': 1, 'tables': {
            's.events': {'partition_columns': ['data_dt'], 'partition_type': 'RANGE'}}}
        (self.root / 'partitions.json').write_text(json.dumps(self.catalog), encoding='utf-8')
        self.rows = []
        statement = "SELECT * FROM s.events WHERE data_dt IN (DATE '20261007', DATE '20261008')"
        for index, sql in enumerate((statement,
                'An exception was encountered during the execution of statement: ' + statement)):
            row = [''] * 33
            row[0], row[2] = '2026-10-01 00:00:{:02d}'.format(index), 'alice'
            row[18], row[21] = 'SELECT', 'statement: ' + sql
            self.rows.append(row)

    def execute(self, source):
        config = {'version': 1, 'source': {'type': source},
                  'partition': {'format': 'config_json', 'path': 'partitions.json'},
                  'output': {'directory': 'runs', 'xlsx': False}}
        if source == 'csv':
            with (self.root / 'source.csv').open('w', encoding='utf-8-sig', newline='') as stream:
                writer = csv.writer(stream)
                writer.writerow(COLUMNS)
                writer.writerows(self.rows)
            config['source']['csv'] = {'path': 'source.csv'}
        else:
            config['source']['mysql'] = {'host': '127.0.0.1', 'user': 'test', 'password': '',
                                        'database': 'logs', 'table': ['day1'], 'batch_rows': 1}
        path = self.root / 'config.json'
        path.write_text(json.dumps(config), encoding='utf-8')
        connection = BatchConnection(BatchCursor({'day1': self.rows}))

        def fake_mysql(*args, **kwargs):
            return run_mysql_analysis(*args, connect=lambda **settings: connection,
                                      password='', workers=1, **kwargs)

        with patch('sql_partition_analysis.pipeline.run_mysql_analysis', side_effect=fake_mysql):
            return run(path)

    def test_default_csv_and_mysql_publish_empty_alerts_and_coverage(self):
        for source in ('csv', 'mysql'):
            with self.subTest(source=source), contextlib.redirect_stdout(io.StringIO()):
                output = self.execute(source)
                self.assertEqual((output / ALERTS_NAME).read_text(encoding='utf-8'), '')
                coverage = json.loads((output / CHECK_NAME).read_text(encoding='utf-8'))
                self.assertEqual(coverage['alert_count'], 0)
                self.assertTrue(coverage['metrics'])
                summary = json.loads((output / 'run_summary.json').read_text(encoding='utf-8'))
                self.assertEqual(summary['status'], 'complete')
                self.assertEqual(summary['partition_omission_alerts'], 0)
                with zipfile.ZipFile(output / '统计结果.zip') as archive:
                    self.assertEqual(json.loads(archive.read('run_summary.json'))['partition_omission_alerts'], 0)
                    self.assertNotIn(ALERTS_NAME, archive.namelist())
                    self.assertNotIn(CHECK_NAME, archive.namelist())

    def test_missing_mapping_warns_and_mysql_run_still_completes(self):
        def lose_mapping(profile, analysis, catalog):
            result = aggregate_accesses(profile, analysis, catalog)
            result['mapping'].pop(0)
            return result

        console = io.StringIO()
        with patch('sql_partition_analysis.mysql_pipeline.aggregate_accesses', side_effect=lose_mapping), \
                contextlib.redirect_stdout(console):
            output = self.execute('mysql')
        alerts = [json.loads(line) for line in (output / ALERTS_NAME).read_text(encoding='utf-8').splitlines()]
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['sql_id'], 'SQL000001')
        self.assertEqual(alerts[0]['table'], 's.events')
        self.assertEqual(alerts[0]['user_name'], 'alice')
        self.assertEqual(alerts[0]['sql'], self.rows[0][21][len('statement: '):])
        self.assertIn('WARNING', console.getvalue())
        summary = json.loads((output / 'run_summary.json').read_text(encoding='utf-8'))
        self.assertEqual(summary['status'], 'complete')
        self.assertEqual(summary['partition_omission_alerts'], 1)
        self.assertTrue((output / '统计结果.zip').is_file())
        self.assertFalse((output / '.mysql_observations.jsonl.gz').exists())

    def test_checker_does_not_require_or_call_the_main_extractor(self):
        from sql_partition_analysis.validation.partition_omissions import check_partition_omissions
        profile = {'widths': {'fixture': 1}, 'sqls': [{
            'id': 'SQL000001', 'sql': "SELECT * FROM s.events WHERE data_dt=DATE '20261007'",
            'count': 1, 'user_name': 'alice', 'first_record': 1,
            'first_time': '2026-10-01 00:00:00', 'last_time': '2026-10-01 00:00:00', 'types': ['SELECT']}]}
        report = {'partition_config': self.catalog, 'mapping': [], 'results': []}
        before = copy.deepcopy((profile, report))
        with patch('sql_partition_analysis.partitioning.analyzer.analyze_profile', side_effect=AssertionError('main extractor called')), \
                patch('sql_partition_analysis.analytics.aggregation.aggregate_accesses', side_effect=AssertionError('main aggregator called')):
            result = check_partition_omissions(profile, report)
        self.assertEqual(len(result['alerts']), 1)
        self.assertEqual((profile, report), before)

    def test_csv_subprocess_warning_reaches_console_and_run_completes(self):
        original_popen = subprocess.Popen

        def damaged_export(command, **kwargs):
            if command[-1] == 'sql_partition_analysis.stages.export_data':
                path = Path(kwargs['env']['SQL_ANALYSIS_OUTPUT_DIR']) / 'aggregate.json'
                aggregate = json.loads(path.read_text(encoding='utf-8'))
                aggregate['mapping'].pop(0)
                path.write_text(json.dumps(aggregate), encoding='utf-8')
            return original_popen(command, **kwargs)

        console = io.StringIO()
        with patch('sql_partition_analysis.pipeline.subprocess.Popen', side_effect=damaged_export), \
                contextlib.redirect_stdout(console):
            output = self.execute('csv')
        alerts = [json.loads(line) for line in (output / ALERTS_NAME).read_text(encoding='utf-8').splitlines()]
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['missing_outputs'], ['mapping'])
        self.assertIn('WARNING 分区访问遗漏检查', console.getvalue())
        summary = json.loads((output / 'run_summary.json').read_text(encoding='utf-8'))
        self.assertEqual(summary['status'], 'complete')
        self.assertEqual(summary['partition_omission_alerts'], 1)
        self.assertTrue((output / '统计结果.zip').is_file())


if __name__ == '__main__':
    unittest.main()
