"""End-to-end contracts for transport/format/engine independence."""
import _support
import contextlib
import csv
from dataclasses import replace
import gzip
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sql_partition_analysis.batch_pipeline import run_event_analysis
from sql_partition_analysis.csv_pipeline import run_csv_analysis
from sql_partition_analysis.ingestion.csv import iter_csv_batches
from sql_partition_analysis.ingestion.formats import get_log_format
from sql_partition_analysis.ingestion.profile import profile_logs
from sql_partition_analysis.ingestion.records import COLUMNS
from sql_partition_analysis.mysql_pipeline import run_mysql_analysis
from sql_partition_analysis.partitioning.analyzer import analyze_profile
from sql_partition_analysis.pipeline import run
from test_mysql_streaming import BatchConnection, BatchCursor


def annotated_analysis(profile, catalog, engine='adb_pg'):
    """A pickleable integration callback used in both serial and worker paths."""
    result = analyze_profile(profile, catalog, engine=engine)
    for row in result['index']:
        row['notes'] += '; custom analyzer used'
    return result


class InputPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.catalog = self.root / 'partitions.json'
        self.catalog.write_text(json.dumps({'version': 1, 'tables': {
            's.events': {'partition_columns': ['dt']}}}))
        query = "SELECT * FROM s.events WHERE dt IN (DATE '2026-10-01', DATE '2026-10-02')"
        values = [('alice', 20, query), ('alice', 10, query), ('alice', 10, query),
                  ('bob', 40, query), ('bob', 50, 'SELECT * FROM s.events')]
        self.rows = []
        for user, second, sql in values:
            row = [''] * 33
            row[0], row[2], row[18], row[21] = (
                '2026-10-01 00:00:{:02d} CST'.format(second), user, 'SELECT', 'statement: ' + sql)
            self.rows.append(row)
        self.source = self.root / 'source.csv'
        self.write_csv(self.rows)

    def write_csv(self, rows):
        with self.source.open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(COLUMNS)
            writer.writerows(rows)

    def config(self):
        return {'version': 1, 'engine': {'type': 'adb_pg'},
                'source': {'type': 'csv', 'log_format': 'adb_legacy_33',
                           'csv': {'path': self.source.name}},
                'partition': {'format': 'config_json', 'path': self.catalog.name},
                'output': {'directory': 'runs', 'xlsx': False}}

    @staticmethod
    def read_json(path):
        return json.loads(path.read_text(encoding='utf-8'))

    def test_csv_and_mysql_share_results_across_batch_sizes_and_workers(self):
        folders = [self.root / name for name in ('serial', 'mysql', 'parallel')]
        for folder in folders:
            folder.mkdir()
        with contextlib.redirect_stdout(io.StringIO()):
            run_csv_analysis(self.source, self.catalog, folders[0], workers=1, batch_rows=2)
            run_csv_analysis(self.source, self.catalog, folders[2], workers=2, batch_rows=5)
            connection = BatchConnection(BatchCursor({'day1': self.rows[:2], 'day2': self.rows[2:]}))
            config = {'host': 'localhost', 'user': 'test', 'database': 'logs',
                      'table': ['day1', 'day2'], 'batch_rows': 1}
            run_mysql_analysis(config, self.root, self.catalog, folders[1], 'fixture',
                               workers=1, connect=lambda **kw: connection, password='')
        self.assertTrue(connection.closed)
        for name in ('analysis.json', 'aggregate.json', 'report_data.json'):
            expected = self.read_json(folders[0] / name)
            for folder in folders[1:]:
                self.assertEqual(self.read_json(folder / name), expected, (folder.name, name))
        for name in ('access_targets.jsonl', 'access_observations.jsonl.gz',
                     '表分区访问统计.csv', '逐条SQL分区识别.jsonl', '逐条日志统计映射.csv.gz'):
            def contents(folder):
                path = folder / name
                return gzip.open(path, 'rb').read() if name.endswith('.gz') else path.read_bytes()
            for folder in folders[1:]:
                self.assertEqual(contents(folder), contents(folders[0]), (folder.name, name))
        expected_profile = json.loads(json.dumps(profile_logs(self.source)))
        self.assertEqual(self.read_json(folders[0] / 'profile.json'), expected_profile)
        groups = self.read_json(folders[0] / 'aggregate.json')['results']
        self.assertEqual(sorted(row['count'] for row in groups), [1, 4, 4])

    def test_explicit_legacy_format_and_engine_reach_input_adapter(self):
        config = self.config()
        config['source']['type'] = 'mysql'
        config['source']['mysql'] = {'host': 'localhost', 'user': 'test', 'database': 'logs', 'table': ['day1']}
        path = self.root / 'config.json'
        path.write_text(json.dumps(config))
        with patch('sql_partition_analysis.pipeline.run_mysql_analysis', side_effect=RuntimeError('stop before reading')) as call, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaisesRegex(RuntimeError, 'stop before reading'):
            run(path)
        self.assertEqual(call.call_args[0][0]['log_format'], 'adb_legacy_33')
        self.assertEqual(call.call_args[1]['engine'], 'adb_pg')
        self.assertEqual(self.read_json(path), config)

    def test_reserved_engines_and_formats_rejected_before_output(self):
        for section, field, value in (('engine', 'type', 'doris'), ('engine', 'type', 'hive'),
                                      ('engine', 'type', 'unknown'), ('source', 'log_format', 'mapped'),
                                      ('source', 'fields', {'sql_text': 'statement'})):
            config = self.config()
            config[section][field] = value
            path = self.root / 'config.json'
            path.write_text(json.dumps(config))
            with self.subTest(value=value), self.assertRaises(ValueError):
                run(path, check_only=True)
            self.assertFalse((self.root / 'runs').exists())

    def test_bad_legacy_record_never_publishes_complete_bundle(self):
        self.write_csv(self.rows + [['too few columns']])
        path = self.root / 'config.json'
        path.write_text(json.dumps(self.config()))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(ValueError):
            run(path)
        output, = (self.root / 'runs').iterdir()
        self.assertEqual(self.read_json(output / 'run_summary.json')['status'], 'failed')
        for name in ('统计结果.zip', '统计结果.zip.part', '.access_observations.jsonl.gz', 'access_manifest.json'):
            self.assertFalse((output / name).exists(), name)

    def test_event_context_rejected_without_analysis_and_stream_closed(self):
        event = get_log_format().to_event(self.rows[0], 1)
        for changes in ({'engine_type': 'hive'}, {'instance_id': 'another'},
                        {'current_database': 'db'}, {'execution_status': 'failed'}):
            output = self.root / next(iter(changes))
            output.mkdir()
            closed = []
            def batches():
                try:
                    yield [replace(event, **changes)]
                finally:
                    closed.append(True)
            with self.subTest(changes=changes), self.assertRaises(ValueError), \
                    patch('sql_partition_analysis.batch_pipeline.analyze_profile') as analyze:
                run_event_analysis(batches(), {}, COLUMNS, self.catalog, output, workers=1)
            analyze.assert_not_called()
            self.assertEqual(closed, [True])
            self.assertEqual(list(output.iterdir()), [])

    def test_parallel_analysis_uses_the_same_callback_as_serial(self):
        outputs = []
        for workers in (1, 2):
            folder = self.root / str(workers)
            folder.mkdir()
            with contextlib.redirect_stdout(io.StringIO()):
                run_event_analysis(iter_csv_batches(self.source), {}, COLUMNS, self.catalog, folder,
                                   workers=workers, analyze=annotated_analysis)
            outputs.append(self.read_json(folder / 'analysis.json'))
        self.assertEqual(outputs[0], outputs[1])
        self.assertTrue(all('custom analyzer used' in row['notes'] for row in outputs[1]['index']))


if __name__ == '__main__':
    unittest.main()
