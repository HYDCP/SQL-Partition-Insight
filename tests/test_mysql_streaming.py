"""MySQL consumes each batch directly while retaining global log semantics."""

import _support

import contextlib
from concurrent.futures import ProcessPoolExecutor
import csv
import gzip
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from sql_partition_analysis import pipeline
from sql_partition_analysis.analytics.aggregation import aggregate_accesses
from sql_partition_analysis.ingestion.mysql import COLUMNS, iter_mysql_batches
from sql_partition_analysis.ingestion.profile import profile_logs
from sql_partition_analysis.mysql_pipeline import run_mysql_analysis
from sql_partition_analysis.partitioning.analyzer import analyze_profile
from sql_partition_analysis.partitioning.catalog import PartitionCatalog
from sql_partition_analysis.reporting.export import export_processed_reports, export_reports


class BatchCursor:
    def __init__(self, tables, before_fetch=None, fail_fetch=None, bad_schema=None):
        self.tables = tables
        self.before_fetch = before_fetch
        self.fail_fetch = fail_fetch
        self.bad_schema = bad_schema
        self.queries = []
        self.fetches = 0

    def execute(self, query, params=None):
        self.queries.append(query)
        self.table = next(name for name in self.tables if query.endswith('`' + name + '`'))
        self.description = [(c,) for c in (COLUMNS[::-1] if self.table == self.bad_schema else COLUMNS)]
        self.rows = self.tables[self.table]
        self.offset = 0

    def fetchmany(self, size):
        self.fetches += 1
        if self.before_fetch:
            self.before_fetch(self)
        if self.fetches == self.fail_fetch:
            raise RuntimeError('interrupted MySQL batch')
        result = self.rows[self.offset:self.offset + size]
        self.offset += len(result)
        return result


class BatchConnection:
    def __init__(self, cursor):
        self.reader = cursor
        self.closed = False

    def cursor(self):
        return self.reader

    def close(self):
        self.closed = True


class MySQLStreamingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mysql-streaming-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.catalog_path = self.root / 'partitions.json'
        self.catalog_path.write_text(json.dumps({
            'version': 1,
            'tables': {
                's.events': {'partition_columns': ['data_dt']},
                's.target': {'partition_columns': ['data_dt']},
            },
            'non_partitioned_tables': ['s.plain'],
        }), encoding='utf-8')
        self.output = self.root / 'mysql'
        self.output.mkdir()

    @staticmethod
    def row(sql, second=0, user='alice', kind='SELECT', prefix='statement: '):
        row = [''] * 33
        row[0] = f'2026-10-01 00:00:{second:02d} CST'
        row[2], row[18], row[21] = user, kind, prefix + sql
        return row

    @staticmethod
    def config(tables, batch_rows=2):
        return dict(host='127.0.0.1', user='test-user', database='log_database',
                    table=list(tables), batch_rows=batch_rows)

    def run_mysql(self, tables, cursor=None, batch_rows=2, workers=1, output=None):
        cursor = cursor or BatchCursor(tables)
        connection = BatchConnection(cursor)
        with contextlib.redirect_stdout(io.StringIO()):
            metrics = run_mysql_analysis(
                self.config(tables, batch_rows), self.root, self.catalog_path,
                output or self.output, 'MySQL fixture', connect=lambda **kw: connection,
                password='', workers=workers)
        return metrics, connection

    @staticmethod
    def read_json(path):
        return json.loads(path.read_text(encoding='utf-8'))

    @staticmethod
    def read_jsonl(path):
        opener = gzip.open if path.suffix == '.gz' else open
        with opener(path, 'rt', encoding='utf-8') as stream:
            return [json.loads(line) for line in stream]

    @staticmethod
    def read_mapping(path):
        with gzip.open(path, 'rt', encoding='utf-8-sig', newline='') as stream:
            return list(csv.DictReader(stream))

    def csv_reference(self, tables):
        output = self.root / 'csv-reference'
        output.mkdir()
        source = output / 'source.csv'
        with source.open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(COLUMNS)
            for rows in tables.values():
                writer.writerows(rows)
        catalog = PartitionCatalog.from_path(self.catalog_path)
        profile = profile_logs(source, 'CSV fixture')
        analysis = analyze_profile(profile, catalog)
        aggregate = aggregate_accesses(profile, analysis, catalog)
        for name, value in [('profile', profile), ('analysis', analysis), ('aggregate', aggregate)]:
            (output / f'{name}.json').write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        export_reports(profile, analysis, aggregate, source, output)
        return output

    def mixed_tables(self):
        sql = "SELECT * FROM s.events WHERE data_dt='2026-09-01'"
        return {
            'day1': [
                self.row(sql, 50),
                self.row(sql, 10, user='bob'),
                self.row(sql, 20, kind='FETCH', prefix='execute p1: '),
                self.row('SELECT * FROM s.plain', 12),
                self.row('connection authorized', 0, kind='', prefix=''),
                self.row("SELECT * FROM s.events a JOIN s.events b ON a.id=b.id "
                         "WHERE a.data_dt='2026-09-03' AND b.data_dt='2026-09-03'", 30),
            ],
            'empty': [],
            'day2': [
                self.row(sql, 5),
                self.row(sql, 45, user='bob', kind='FETCH'),
                self.row(sql, 5),
                self.row("SELECT * FROM s.target WHERE data_dt IN ('2026-09-01','2026-09-02')", 40),
                self.row("SELECT * FROM s.target WHERE data_dt='2026-09-04'", 25, user='bob'),
                self.row('SELECT * FROM events', 15),
            ],
        }

    def assert_no_raw_source_or_spool(self):
        for name in ('源日志.csv', '源日志.csv.part', '.mysql_observations.jsonl.gz'):
            self.assertFalse((self.output / name).exists(), name)

    def test_cross_batch_global_statistics_and_reports_match_csv_reference(self):
        tables = self.mixed_tables()
        reference = self.csv_reference(tables)
        with patch('sql_partition_analysis.mysql_pipeline.analyze_profile', wraps=analyze_profile) as analyze:
            metrics, connection = self.run_mysql(tables)
        self.assertTrue(connection.closed)
        self.assertEqual(len(connection.reader.queries), 3)
        self.assert_no_raw_source_or_spool()

        for name in ('profile.json', 'analysis.json', 'aggregate.json', 'report_data.json'):
            actual = self.read_json(self.output / name)
            expected = self.read_json(reference / name)
            if name == 'profile.json':
                actual.pop('source')
                expected.pop('source')
            self.assertEqual(actual, expected, name)
        self.assertEqual(metrics, self.read_json(reference / 'aggregate.json')['metrics'])

        for name in ('表分区访问统计.csv', 'SQL解析明细.csv', 'SQL原文索引.csv',
                     '待确认字段.csv', '表分区配置核对.csv'):
            self.assertEqual((self.output / name).read_text(encoding='utf-8-sig'),
                             (reference / name).read_text(encoding='utf-8-sig'), name)
        self.assertEqual(self.read_mapping(self.output / '逐条日志统计映射.csv.gz'),
                         self.read_mapping(reference / '逐条日志统计映射.csv.gz'))
        for name in ('access_targets.jsonl', 'access_observations.jsonl.gz'):
            self.assertEqual(self.read_jsonl(self.output / name), self.read_jsonl(reference / name), name)
        actual = self.read_json(self.output / 'access_manifest.json')
        expected = self.read_json(reference / 'access_manifest.json')
        for value in (actual, expected):
            value.pop('source')
            value.pop('run_id')
        self.assertEqual(actual, expected)

        profile = self.read_json(self.output / 'profile.json')
        parsed_ids = [record['id'] for call in analyze.call_args_list
                      for record in (call[0][0] if call[0] else call[1]['profile'])['sqls']]
        self.assertEqual(parsed_ids, [record['id'] for record in profile['sqls']])
        sql = "SELECT * FROM s.events WHERE data_dt='2026-09-01'"
        users = {record['user_name']: record for record in profile['sqls'] if record['sql'] == sql}
        self.assertEqual({user: value['count'] for user, value in users.items()}, {'alice': 4, 'bob': 2})
        self.assertEqual(users['alice']['first_record'], 1)
        self.assertEqual(users['alice']['types'], ['FETCH', 'SELECT'])
        self.assertEqual(users['alice']['first_time'], '2026-10-01 00:00:05 CST')
        self.assertEqual(users['alice']['last_time'], '2026-10-01 00:00:50 CST')
        aggregate = self.read_json(self.output / 'aggregate.json')
        repeated, = [row for row in aggregate['results']
                     if row['table'] == 's.events' and row['partition'] == 'data_dt=2026-09-01']
        self.assertEqual((repeated['count'], repeated['mean_seconds']), (6, 9))
        self_join, = [row for row in aggregate['results']
                      if row['table'] == 's.events' and row['partition'] == 'data_dt=2026-09-03']
        self.assertEqual(self_join['count'], 1)
        self.assertEqual(metrics['source_rows'], 12)
        observations = self.read_jsonl(self.output / 'access_observations.jsonl.gz')
        self.assertEqual([row['source_record'] for row in observations], [1, 2, 3, 6, 7, 8, 9, 10, 11])
        self.assertEqual(observations[4]['log_time'], observations[6]['log_time'])
        self.assertEqual(observations[4]['sql_id'], observations[6]['sql_id'])
        self.assertEqual(observations[4]['access_ids'], observations[6]['access_ids'])

    def test_first_batch_is_analyzed_before_next_fetch(self):
        first = self.row("SELECT * FROM s.events WHERE data_dt='2026-09-01'", 10)
        second = self.row("SELECT * FROM s.events WHERE data_dt='2026-09-02'", 20)
        tables = {'day1': [first, second]}

        with patch('sql_partition_analysis.mysql_pipeline.analyze_profile', wraps=analyze_profile) as analyze:
            def before_fetch(cursor):
                if cursor.fetches == 2:
                    self.assertGreater(analyze.call_count, 0,
                                       '首批数据必须在后续 fetchmany 之前完成解析')
                    call = analyze.call_args
                    records = (call[0][0] if call[0] else call[1]['profile'])['sqls']
                    self.assertEqual([record['sql'] for record in records], [first[21][len('statement: '):]])
            cursor = BatchCursor(tables, before_fetch=before_fetch)
            self.run_mysql(tables, cursor=cursor, batch_rows=1)
        self.assert_no_raw_source_or_spool()

    def test_two_workers_reuse_pool_and_match_serial_global_results(self):
        tables = self.mixed_tables()
        serial = self.root / 'mysql-serial'
        serial.mkdir()
        expected_metrics, serial_connection = self.run_mysql(tables, output=serial)
        with patch('sql_partition_analysis.mysql_pipeline.ProcessPoolExecutor', wraps=ProcessPoolExecutor) as pool:
            metrics, parallel_connection = self.run_mysql(tables, workers=2)
        self.assertEqual(pool.call_count, 1, '多批次应复用同一解析进程池')
        self.assertTrue(serial_connection.closed)
        self.assertTrue(parallel_connection.closed)
        self.assertEqual(metrics, expected_metrics)
        for name in ('profile.json', 'analysis.json', 'aggregate.json', 'report_data.json'):
            self.assertEqual(self.read_json(self.output / name), self.read_json(serial / name), name)
        for name in ('access_targets.jsonl', 'access_observations.jsonl.gz'):
            self.assertEqual(self.read_jsonl(self.output / name), self.read_jsonl(serial / name), name)
        self.assertEqual(self.read_mapping(self.output / '逐条日志统计映射.csv.gz'),
                         self.read_mapping(serial / '逐条日志统计映射.csv.gz'))
        self.assert_no_raw_source_or_spool()

    def test_temporary_observations_contain_only_compact_record_evidence(self):
        tables = {'day1': [self.row("SELECT * FROM s.events WHERE data_dt='2026-09-01'", 10),
                           self.row("SELECT * FROM s.events WHERE data_dt='2026-09-02'", 20)]}
        compact = bytearray()
        original_write = gzip.GzipFile.write

        def capture_write(stream, data):
            if Path(stream.name).name == '.mysql_observations.jsonl.gz':
                compact.extend(data)
            return original_write(stream, data)

        with patch('gzip.GzipFile.write', new=capture_write):
            self.run_mysql(tables, batch_rows=1)
        evidence = [json.loads(line) for line in compact.decode('utf-8').splitlines()]
        self.assertEqual([row['source_record'] for row in evidence], [1, 2])
        self.assertTrue(all(set(row) == {'source_record', 'log_time', 'sql_id'} for row in evidence))
        self.assertEqual([row['log_time'] for row in evidence], [row[0] for row in tables['day1']])
        self.assert_no_raw_source_or_spool()

    def test_read_failure_closes_connection_and_cleans_temporary_evidence(self):
        tables = {'day1': [self.row("SELECT * FROM s.events WHERE data_dt='2026-09-01'", 10),
                           self.row("SELECT * FROM s.events WHERE data_dt='2026-09-02'", 20)]}
        connection = BatchConnection(BatchCursor(tables, fail_fetch=2))
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, 'interrupted MySQL batch'):
            run_mysql_analysis(self.config(tables, 1), self.root, self.catalog_path,
                               self.output, 'MySQL fixture', connect=lambda **kw: connection,
                               password='', workers=1)
        self.assertTrue(connection.closed)
        self.assert_no_raw_source_or_spool()
        self.assertFalse((self.output / 'access_manifest.json').exists())

    def test_analysis_failure_closes_connection_and_cleans_temporary_evidence(self):
        tables = {'day1': [self.row("SELECT * FROM s.events WHERE data_dt='2026-09-01'", 10),
                           self.row("SELECT * FROM s.events WHERE data_dt='2026-09-02'", 20)]}
        connection = BatchConnection(BatchCursor(tables))
        with patch('sql_partition_analysis.mysql_pipeline.analyze_profile', side_effect=RuntimeError('parser worker failed')), \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, 'parser worker failed'):
            run_mysql_analysis(self.config(tables, 1), self.root, self.catalog_path,
                               self.output, 'MySQL fixture', connect=lambda **kw: connection,
                               password='', workers=1)
        self.assertTrue(connection.closed)
        self.assert_no_raw_source_or_spool()
        self.assertFalse((self.output / 'access_manifest.json').exists())

    def test_pymysql_active_result_abort_detaches_before_close_without_draining_socket(self):
        from pymysql.connections import MySQLResult
        from pymysql.cursors import SSCursor

        class AbortConnection:
            def __init__(self):
                self.closed = False
                self.network_reads = 0
                self.state_at_close = None

            def cursor(self):
                return cursor

            def _read_packet(self, *args, **kwargs):
                self.network_reads += 1
                raise AssertionError('中止后不可继续读取或排空 MySQL 结果')

            def close(self):
                self.state_at_close = (cursor.connection, result.connection, result.unbuffered_active)
                self.closed = True

        connection = AbortConnection()
        cursor = SSCursor(connection)
        result = MySQLResult(connection)
        result.unbuffered_active = True
        result.has_next = False
        cursor._result = connection._result = result
        cursor.description = [(name,) for name in COLUMNS]
        row = self.row("SELECT * FROM s.events WHERE data_dt='2026-09-01'", 10)
        try:
            with patch.object(cursor, 'execute', return_value=1), \
                    patch.object(cursor, 'fetchmany', return_value=[row]) as fetch, \
                    patch('sql_partition_analysis.mysql_pipeline.analyze_profile', side_effect=RuntimeError('consumer parser failed')), \
                    contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, 'consumer parser failed'):
                run_mysql_analysis(self.config(['day1'], 1), self.root, self.catalog_path,
                                   self.output, 'MySQL fixture', connect=lambda **kw: connection,
                                   password='', workers=1)
            fetch.assert_called_once_with(1)
            self.assertTrue(connection.closed)
            self.assertIsNone(cursor.connection)
            self.assertIsNone(result.connection)
            self.assertFalse(result.unbuffered_active)
            self.assertEqual(connection.state_at_close, (None, None, False))
            with patch.object(result, '_finish_unbuffered_query', wraps=result._finish_unbuffered_query) as finish:
                cursor.close()
                cursor.__del__()
                result.__del__()
            finish.assert_not_called()
            self.assertEqual(connection.network_reads, 0)
            self.assert_no_raw_source_or_spool()
        finally:
            # Prevent noisy unraisable destructors even when testing broken cleanup.
            cursor.connection = None
            result.unbuffered_active = False
            result.connection = None

    def test_mysql_batch_transport_normalizes_rows_and_preserves_duplicates(self):
        row = self.row('SELECT 1', 10)
        row[2], row[30], row[31] = None, None, 42
        tables = {'day1': [row, row], 'empty': [], 'day2': [row]}
        connection = BatchConnection(BatchCursor(tables))
        with contextlib.redirect_stdout(io.StringIO()):
            batches = list(iter_mysql_batches(self.config(tables, 1), self.root,
                                             connect=lambda **kw: connection, password=''))
        self.assertEqual([len(batch) for batch in batches], [1, 1, 1])
        normalized = ['' if value is None else str(value) for value in row]
        self.assertEqual([record for batch in batches for record in batch], [normalized] * 3)
        self.assertTrue(connection.closed)

    def test_later_table_schema_failure_does_not_publish_access_manifest(self):
        row = self.row("SELECT * FROM s.events WHERE data_dt='2026-09-01'", 10)
        tables = {'day1': [row], 'day2': [row]}
        connection = BatchConnection(BatchCursor(tables, bad_schema='day2'))
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ValueError):
            run_mysql_analysis(self.config(tables, 1), self.root, self.catalog_path,
                               self.output, 'MySQL fixture', connect=lambda **kw: connection,
                               password='', workers=1)
        self.assertTrue(connection.closed)
        self.assert_no_raw_source_or_spool()
        self.assertFalse((self.output / 'access_manifest.json').exists())

    def test_all_empty_tables_fail_without_access_manifest(self):
        tables = {'empty1': [], 'empty2': []}
        connection = BatchConnection(BatchCursor(tables))
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ValueError):
            run_mysql_analysis(self.config(tables), self.root, self.catalog_path,
                               self.output, 'MySQL fixture', connect=lambda **kw: connection,
                               password='', workers=1)
        self.assertTrue(connection.closed)
        self.assert_no_raw_source_or_spool()
        self.assertFalse((self.output / 'access_manifest.json').exists())

    def pipeline_config(self, tables):
        path = self.root / 'config.json'
        path.write_text(json.dumps({
            'version': 1,
            'source': {'type': 'mysql', 'mysql': dict(self.config(tables, 1), password='')},
            'partition': {'format': 'config_json', 'path': self.catalog_path.name},
            'output': {'directory': 'runs', 'xlsx': False},
        }), encoding='utf-8')
        return path

    def test_pipeline_mysql_uses_independent_chain_and_publishes_complete_bundle(self):
        tables = self.mixed_tables()
        connection = BatchConnection(BatchCursor(tables))
        path = self.pipeline_config(tables)

        def run_with_fake_connection(*args, **kwargs):
            kwargs.update(connect=lambda **kw: connection, password='', workers=1)
            return run_mysql_analysis(*args, **kwargs)

        with patch('sql_partition_analysis.pipeline.run_mysql_analysis', side_effect=run_with_fake_connection) as mysql_run, \
                patch('sql_partition_analysis.pipeline.subprocess.Popen', side_effect=AssertionError('CSV stage invoked')), \
                contextlib.redirect_stdout(io.StringIO()):
            output = pipeline.run(path)
        mysql_run.assert_called_once()
        self.assertTrue(connection.closed)
        summary = self.read_json(output / 'run_summary.json')
        self.assertEqual((summary['status'], summary['source_type'], summary['source_rows']), ('complete', 'mysql', 12))
        self.assertEqual(summary['database_tables'], ['log_database.day1', 'log_database.empty', 'log_database.day2'])
        for name in ('源日志.csv', '源日志.csv.part', '.mysql_observations.jsonl.gz'):
            self.assertFalse((output / name).exists(), name)
        with zipfile.ZipFile(output / '统计结果.zip') as archive:
            self.assertIsNone(archive.testzip())
            self.assertEqual(json.loads(archive.read('run_summary.json'))['status'], 'complete')
            self.assertIn('access_observations.jsonl.gz', archive.namelist())

    def test_pipeline_stream_failure_marks_run_failed_without_complete_bundle(self):
        tables = {'day1': [self.row("SELECT * FROM s.events WHERE data_dt='2026-09-01'", 10),
                           self.row("SELECT * FROM s.events WHERE data_dt='2026-09-02'", 20)]}
        connection = BatchConnection(BatchCursor(tables, fail_fetch=2))
        path = self.pipeline_config(tables)

        def run_with_fake_connection(*args, **kwargs):
            kwargs.update(connect=lambda **kw: connection, password='', workers=1)
            return run_mysql_analysis(*args, **kwargs)

        with patch('sql_partition_analysis.pipeline.run_mysql_analysis', side_effect=run_with_fake_connection), \
                patch('sql_partition_analysis.pipeline.subprocess.Popen', side_effect=AssertionError('CSV stage invoked')), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaisesRegex(RuntimeError, 'interrupted MySQL batch'):
            pipeline.run(path)
        output, = (self.root / 'runs').iterdir()
        self.assertEqual(self.read_json(output / 'run_summary.json')['status'], 'failed')
        self.assertTrue(connection.closed)
        for name in ('统计结果.zip', '统计结果.zip.part', 'access_manifest.json', '源日志.csv',
                     '源日志.csv.part', '.mysql_observations.jsonl.gz'):
            self.assertFalse((output / name).exists(), name)

    def test_missing_access_evidence_fails_reconciliation_and_cleans_journal(self):
        sql = "SELECT * FROM s.events WHERE data_dt='2026-09-01'"
        tables = {'day1': [self.row(sql, 10), self.row(sql, 20)]}
        connection = BatchConnection(BatchCursor(tables))
        path = self.pipeline_config(tables)

        def run_with_fake_connection(*args, **kwargs):
            kwargs.update(connect=lambda **kw: connection, password='', workers=1)
            return run_mysql_analysis(*args, **kwargs)

        def export_with_missing_observation(profile, analysis, aggregate, observations, output, progress=None):
            observations = iter(observations)
            next(observations)
            return export_processed_reports(profile, analysis, aggregate, observations, output, progress)

        with patch('sql_partition_analysis.pipeline.run_mysql_analysis', side_effect=run_with_fake_connection), \
                patch('sql_partition_analysis.mysql_pipeline.export_processed_reports', side_effect=export_with_missing_observation), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaisesRegex(ValueError, '逐条访问证据与统计组不一致'):
            pipeline.run(path)
        output, = (self.root / 'runs').iterdir()
        self.assertEqual(self.read_json(output / 'run_summary.json')['status'], 'failed')
        self.assertTrue(connection.closed)
        for name in ('统计结果.zip', '统计结果.zip.part', 'access_manifest.json', '.mysql_observations.jsonl.gz'):
            self.assertFalse((output / name).exists(), name)


if __name__ == '__main__':
    unittest.main()
