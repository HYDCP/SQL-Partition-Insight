import _support
import csv
import contextlib
import gzip
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from sql_partition_analysis.ingestion.mysql import COLUMNS, mysql_queries, stream_mysql
from sql_partition_analysis.configuration import load_config, validate
from sql_partition_analysis.pipeline import run
from sql_partition_analysis.mysql_pipeline import run_mysql_analysis


class Cursor:
    description = [(c,) for c in COLUMNS]

    def __init__(self, rows, fail=False):
        self.rows = rows
        self.offset = 0
        self.fail = fail

    def execute(self, query, params=None):
        self.query, self.params = query, params

    def fetchmany(self, size):
        if self.fail and self.offset:
            raise RuntimeError('interrupted stream')
        batch = self.rows[self.offset:self.offset + size]
        self.offset += len(batch)
        return batch


class Connection:
    def __init__(self, cursor):
        self.reader = cursor
        self.closed = False

    def cursor(self):
        return self.reader

    def close(self):
        self.closed = True


class TableCursor(Cursor):
    def __init__(self, tables, bad_schema=None, fail_table=None):
        super().__init__([])
        self.tables, self.bad_schema, self.fail_table = tables, bad_schema, fail_table
        self.queries = []

    def execute(self, query, params=None):
        self.queries.append(query)
        table = next(name for name in self.tables if query.endswith('`' + name + '`'))
        if table == self.fail_table:
            raise RuntimeError('missing or inaccessible table')
        self.description = [(c,) for c in (COLUMNS[::-1] if table == self.bad_schema else COLUMNS)]
        self.rows, self.offset = self.tables[table], 0


class RunnerTests(unittest.TestCase):
    def test_version_check_follows_edited_requirements_and_reports_installed_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'partitions.json').write_text('{"version":1,"tables":{}}')
            path = root / 'config.json'
            path.write_text(json.dumps({
                'version': 1, 'source': {'type': 'mysql', 'mysql': self.config()},
                'partition': {'format': 'config_json', 'path': 'partitions.json'},
                'output': {'directory': 'runs'}}))
            requirements = root / 'requirements.txt'
            requirements.write_text('SQLGlot == 26.9.1  # deployment pin\nPyMySQL==1.1.1\n')
            with patch('sql_partition_analysis.pipeline.PROJECT_ROOT', root), \
                    patch('sqlglot.__version__', '26.9.2'), \
                    patch('sql_partition_analysis.pipeline.run_mysql_analysis') as transport:
                with self.assertRaises(ValueError) as failure:
                    run(path, check_only=True)
                message = str(failure.exception)
                self.assertIn('要求 sqlglot==26.9.1', message)
                self.assertIn('实际安装 26.9.2', message)
                self.assertNotIn('26.9.0', message)
                requirements.write_text('sqlglot==26.9.2\n')
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertIsNone(run(path, check_only=True))
                self.assertIn('SQLGlot=26.9.2', output.getvalue())
                transport.assert_not_called()
            self.assertFalse((root / 'runs').exists())

    def test_multiple_tables_allow_empty_table_and_preserve_duplicates(self):
        row = self.row()
        row[2] = None
        cursor = TableCursor({'day1': [row, row], 'empty': [], 'day2': [row]})
        conn = Connection(cursor)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'logs.csv'
            config = dict(self.config(), table=['day1', 'empty', 'day2'])
            self.assertEqual(stream_mysql(config, target, Path(tmp), connect=lambda **kw: conn, password=''), 3)
            with target.open(encoding='utf-8-sig', newline='') as f:
                rows = list(csv.reader(f))
            self.assertEqual(len(rows), 4)
            self.assertEqual(rows[0], COLUMNS)
            self.assertEqual(rows[1][21], row[21])
            self.assertEqual((rows[1][2], rows[1][30]), ('', ''))
            self.assertEqual(rows[1], rows[2])
            self.assertEqual(rows[2], rows[3])
            self.assertEqual(len(cursor.queries), 3)
            self.assertTrue(all(q.startswith('SELECT `column1`, `column2`, `column3`') for q in cursor.queries))
            self.assertTrue(all('WHERE' not in q and 'UNION' not in q for q in cursor.queries))
            self.assertTrue(conn.closed)

    def test_later_table_failure_never_publishes_partial_export(self):
        for option in ('bad_schema', 'fail_table'):
            with self.subTest(option=option), tempfile.TemporaryDirectory() as tmp:
                cursor = TableCursor({'day1': [self.row()], 'day2': [self.row()]}, **{option: 'day2'})
                conn = Connection(cursor)
                target = Path(tmp) / 'logs.csv'
                target.write_text('previous successful export')
                with self.assertRaises((ValueError, RuntimeError)):
                    stream_mysql(dict(self.config(), table=['day1', 'day2']), target, Path(tmp),
                                 connect=lambda **kw: conn, password='')
                self.assertEqual(target.read_text(), 'previous successful export')
                self.assertFalse(target.with_suffix('.csv.part').exists())
                self.assertTrue(conn.closed)

    def test_mysql_multiday_tables_aggregate_counts_and_intervals_together(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            row1 = self.row()
            row1[21] = "statement: SELECT * FROM s.events WHERE data_dt='2026-10-01'"
            row2 = list(row1)
            row2[0] = '2026-10-09 06:55:03 CST'
            cursor = TableCursor({'day1': [row1, row1], 'day2': [row2, row2]})
            conn = Connection(cursor)
            (root / 'partition.json').write_text(json.dumps({'version': 1, 'tables': {
                's.events': {'partition_columns': ['data_dt']}}}))
            config = {'version': 1, 'source': {'type': 'mysql', 'mysql': dict(self.config(), table=['day1', 'day2'])},
                      'partition': {'format': 'config_json', 'path': 'partition.json'},
                      'output': {'directory': 'runs'}}
            path = root / 'config.json'
            path.write_text(json.dumps(config))
            def fake_analysis(settings, base, catalog, output, label, **kwargs):
                return run_mysql_analysis(settings, base, catalog, output, label,
                                          connect=lambda **kw: conn, password='', workers=1, **kwargs)
            with patch('sql_partition_analysis.pipeline.run_mysql_analysis', side_effect=fake_analysis), contextlib.redirect_stdout(io.StringIO()):
                output = run(path)
            rows = json.loads((output / 'aggregate.json').read_text())['results']
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['count'], 4)
            self.assertEqual(rows[0]['mean_seconds'], 28800)
            summary = json.loads((output / 'run_summary.json').read_text())
            self.assertEqual(summary['database_tables'], ['pg_log_db_v3.day1', 'pg_log_db_v3.day2'])
            self.assertNotIn('date_range', summary)
            with gzip.open(output / 'access_observations.jsonl.gz', 'rt', encoding='utf-8') as stream:
                observations = [json.loads(line) for line in stream]
            self.assertEqual([row['source_record'] for row in observations], [1, 2, 3, 4])
            self.assertEqual([row['log_time'] for row in observations], [row1[0], row1[0], row2[0], row2[0]])
            self.assertEqual(len({tuple(row['access_ids']) for row in observations}), 1)
            target, = [json.loads(line) for line in (output / 'access_targets.jsonl').read_text().splitlines()]
            self.assertEqual(target['operation'], 'read')
            self.assertEqual(target['partition_condition'], 'data_dt=2026-10-01')
            self.assertEqual(observations[0]['access_ids'], [target['access_id']])
            self.assertIsNone(target['physical_partition_id'])
            manifest = json.loads((output / 'access_manifest.json').read_text())
            self.assertEqual((manifest['source_records'], manifest['included_records']), (4, 4))
            self.assertEqual(manifest['log_coverage'], 'unknown')

    def test_all_csv_outputs_only_include_configured_partitioned_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mixed = "SELECT * FROM s.p p JOIN s.n n ON p.id=n.id WHERE p.data_dt='2026-09-01'"
            statements = ['SELECT * FROM s.n', mixed, 'SELECT * FROM s.outside', mixed,
                          'SELECT * FROM s.p', 'SELECT * FROM p']
            with (root / 'source.csv').open('w', newline='') as stream:
                writer = csv.writer(stream)
                writer.writerow(COLUMNS)
                for i, sql in enumerate(statements):
                    row = [''] * 33
                    row[0], row[2], row[18], row[21] = (
                        f'2026-10-01 00:00:{i * 5:02d} CST', 's', 'SELECT', 'statement: ' + sql)
                    writer.writerow(row)
            (root / 'partitions.json').write_text(json.dumps({
                'version': 1, 'tables': {'s.p': {'partition_columns': ['data_dt']}},
                'non_partitioned_tables': ['s.n']}))
            config = {'version': 1, 'source': {'type': 'mysql', 'csv': {'path': 'source.csv'}, 'mysql': {}},
                      'partition': {'format': 'config_json', 'path': 'partitions.json'},
                      'output': {'directory': 'runs', 'xlsx': False}}
            path = root / 'config.json'
            path.write_text(json.dumps(config))
            result = subprocess.run([sys.executable, str(_support.CLI),
                                     '--source', 'csv', '--config', str(path)], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(json.loads(path.read_text()), config)
            output, = (root / 'runs').iterdir()
            def rows(name):
                with (output / name).open(encoding='utf-8-sig', newline='') as stream:
                    return list(csv.DictReader(stream))
            for name in ('表分区访问统计.csv', 'SQL解析明细.csv', '表分区配置核对.csv'):
                self.assertEqual({r['表名'] for r in rows(name)}, {'s.p'}, name)
            stats = rows('表分区访问统计.csv')
            self.assertEqual(sorted(int(r['访问次数']) for r in stats), [1, 2])
            unknown, = [r for r in stats if r['访问次数'] == '1']
            self.assertIn('未识别到 data_dt 的筛选条件', unknown['分区或完整条件'])
            self.assertEqual(unknown['平均访问间隔_秒'], '')
            repeated, = [r for r in stats if r['访问次数'] == '2']
            self.assertEqual(float(repeated['平均访问间隔_秒']), 10)
            index = rows('SQL原文索引.csv')
            self.assertEqual({r['原始SQL正文'] for r in index}, {mixed, 'SELECT * FROM s.p'})
            with gzip.open(output / '逐条日志统计映射.csv.gz', 'rt', encoding='utf-8-sig') as stream:
                mapping = list(csv.DictReader(stream))
            self.assertEqual([int(r['源数据记录序号_不含表头']) for r in mapping], [2, 4, 5])
            self.assertTrue(all(r['统计组ID'] for r in mapping))
            metrics = json.loads((output / 'aggregate.json').read_text())['metrics']
            self.assertEqual((metrics['source_rows'], metrics['tables'], metrics['access_log_count']), (6, 1, 3))

    def test_source_override_only_validates_selected_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'partitions.json').write_text('{"version":1,"tables":{}}')
            path = root / 'config.json'
            config = {'version': 1, 'source': {'type': 'csv', 'csv': {'path': 'missing.csv'},
                       'mysql': dict(self.config(), password='')},
                      'partition': {'format': 'config_json', 'path': 'partitions.json'},
                      'output': {'directory': 'runs', 'xlsx': False}}
            path.write_text(json.dumps(config))
            validate(load_config(path, 'mysql'), root)
            self.assertEqual(load_config(path)['source']['type'], 'csv')
            with self.assertRaisesRegex(ValueError, 'CSV 输入文件不存在'):
                validate(load_config(path), root)
            for mode, expected_code in [('mysql', 0), ('csv', 1), ('invalid', 2)]:
                result = subprocess.run([sys.executable, str(_support.CLI),
                                         '--config', str(path), '--source', mode, '--check'],
                                        cwd='/', capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, expected_code, result.stdout + result.stderr)
            self.assertFalse((root / 'runs').exists())

    def test_password_from_config_with_environment_override_and_empty_password(self):
        for configured, environment, expected in [('configured-test', {}, 'configured-test'),
                                                  ('configured-test', {'CUSTOM_DB_SECRET': 'env-test'}, 'env-test'),
                                                  ('', {}, '')]:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as tmp:
                kwargs_seen = {}
                def connect(**kwargs):
                    kwargs_seen.update(kwargs)
                    return Connection(Cursor([self.row()]))
                config = dict(self.config(), password=configured, password_env='CUSTOM_DB_SECRET')
                with patch.dict(os.environ, environment, clear=True):
                    stream_mysql(config, Path(tmp) / 'logs.csv', Path(tmp), connect=connect)
                self.assertEqual(kwargs_seen['password'], expected)

    def test_configured_password_redacted_on_connection_failure(self):
        def fail(**kwargs):
            raise RuntimeError('connection failed: configured-secret-test')
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError) as result:
                stream_mysql(dict(self.config(), password='configured-secret-test'),
                             Path(tmp) / 'logs.csv', Path(tmp), connect=fail)
            self.assertNotIn('configured-secret-test', str(result.exception))

    def config(self):
        return dict(host='127.0.0.1', port=9030, user='root', database='pg_log_db_v3',
                    table=['pg_slow_query_log'], batch_rows=1)

    def row(self):
        row = [''] * 33
        row[0], row[2], row[18] = '2026-10-08 06:55:03 CST', 'bdm', 'INSERT'
        row[21] = "statement: SELECT '含中文,逗号\n双引号\"和控制符\x18'"
        row[30] = None
        return row

    def test_rejects_invalid_tables_and_obsolete_date_options(self):
        for patch in ({'table': 'logs; DROP TABLE logs'},
                      {'order_by': ['column1 DESC;']},
                      {'table': []},
                      {'table': ['one', 'one']},
                      {'table': ['one', 'two; DROP TABLE one']},
                      {'table': [None]},
                      {'table': {'name': 'logs'}},
                      {'end_date_exclusive': '2026-10-08'},
                      {'start_date': '2026-02-30'},
                      {'columns': ['column1'] * 33}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                mysql_queries(dict(self.config(), **patch))

    def test_failed_stream_never_publishes_partial_csv(self):
        conn = Connection(Cursor([self.row(), self.row()], fail=True))
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'logs.csv'
            with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                stream_mysql(self.config(), target, Path(tmp),
                             connect=lambda **kw: conn, password='test-only')
            self.assertFalse(target.exists())
            self.assertFalse(target.with_suffix('.csv.part').exists())
            self.assertTrue(conn.closed)

    def test_no_rows_or_null_required_field_fails(self):
        bad = self.row()
        bad[0] = None
        for rows in ([], [bad]):
            with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError):
                stream_mysql(self.config(), Path(tmp) / 'logs.csv', Path(tmp),
                             connect=lambda **kw: Connection(Cursor(rows)), password='test-only')


if __name__ == '__main__':
    unittest.main()
