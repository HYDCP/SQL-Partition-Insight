"""Input adapters preserve ADB compatibility and source occurrence semantics."""
import _support

import contextlib
import csv
from dataclasses import FrozenInstanceError, replace
import io
from pathlib import Path
import tempfile
import unittest

from sql_partition_analysis.ingestion.csv import iter_csv_batches
from sql_partition_analysis.ingestion.formats import LogFormat, get_log_format
from sql_partition_analysis.ingestion.mysql import iter_mysql_event_batches, mysql_queries
from sql_partition_analysis.ingestion.profile import profile_logs
from sql_partition_analysis.ingestion.records import COLUMNS
from sql_partition_analysis.ingestion.state import LogProfileState


class LogInputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write_csv(self, header, rows):
        path = self.root / 'logs.csv'
        with path.open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(header)
            writer.writerows(rows)
        return path

    @staticmethod
    def legacy_row(raw_sql, timestamp, user='alice', kind='SELECT'):
        row = [''] * 33
        row[0], row[2], row[18], row[21] = timestamp, user, kind, raw_sql
        return row

    def test_legacy_profile_exact_shape_and_duplicates(self):
        late = '2026-10-01 00:00:20 CST'
        early = '2026-10-01 00:00:10 CST'
        header = ['custom{}'.format(i) for i in range(33)]
        rows = [self.legacy_row('statement: SELECT 1', late),
                self.legacy_row('execute p1: SELECT 1', early, kind='FETCH'),
                self.legacy_row('statement: SELECT 1', early, user='bob'),
                self.legacy_row('statement: SELECT 1', late)]
        path = self.write_csv(header, rows)
        profile = profile_logs(path, 'fixture')
        self.assertEqual(profile, {
            'source': {'path': str(path), 'label': 'fixture'},
            'header': header, 'widths': {33: 4},
            'types': {'SELECT': 3, 'FETCH': 1}, 'bad': [],
            'sqls': [
                {'id': 'SQL000001', 'sql': 'SELECT 1', 'user_name': 'alice',
                 'count': 3, 'first_record': 1, 'first_time': early,
                 'last_time': late, 'types': ['FETCH', 'SELECT']},
                {'id': 'SQL000002', 'sql': 'SELECT 1', 'user_name': 'bob',
                 'count': 1, 'first_record': 3, 'first_time': early,
                 'last_time': early, 'types': ['SELECT']},
            ],
        })
        state = LogProfileState(profile['source'], header)
        occurrences = []
        for batch in iter_csv_batches(path, batch_rows=1):
            _, observations = state.add_batch(batch)
            occurrences.extend(observations)
        self.assertEqual(state.profile(), profile)
        self.assertEqual([row['source_record'] for row in occurrences], [1, 2, 3, 4])
        self.assertEqual([row['sql_id'] for row in occurrences],
                         ['SQL000001', 'SQL000001', 'SQL000002', 'SQL000001'])

    def test_legacy_event_is_immutable_and_preserves_raw_multiline_sql(self):
        sql = "SELECT 'a,b'\nFROM events"
        raw = 'statement: ' + sql
        path = self.write_csv(COLUMNS, [self.legacy_row(raw, '2026-10-01 00:00:00 CST')])
        event = list(iter_csv_batches(path))[0][0]
        self.assertEqual(event.sql_text, sql)
        self.assertEqual(event.raw_sql, raw)
        self.assertEqual(event.user_name, 'alice')
        self.assertEqual(event.source_width, 33)
        with self.assertRaises(FrozenInstanceError):
            event.sql_text = 'changed'

    def test_unimplemented_formats_fail_before_reading_or_connecting(self):
        self.assertIsInstance(get_log_format(), LogFormat)
        for name in ('mapped', 'canonical_v1', 'unknown'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                get_log_format(name)
            with self.subTest(name=name), self.assertRaises(ValueError):
                list(iter_csv_batches(self.root / 'missing.csv', log_format=name))
        for unsupported in ({'log_format': 'mapped'}, {'fields': {}}):
            config = dict(host='localhost', user='test', database='logs', table=['day1'])
            config.update(unsupported)
            with self.subTest(config=unsupported), self.assertRaises(ValueError):
                list(iter_mysql_event_batches(config, self.root,
                     connect=lambda **kwargs: self.fail('must fail before connection'), password=''))

    def test_bad_legacy_rows_and_empty_input_fail(self):
        for rows in ([], [['too few']], [[''] * 33]):
            path = self.write_csv(COLUMNS, rows)
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                profile_logs(path)
        (self.root / 'empty.csv').write_text('')
        with self.assertRaises(ValueError):
            profile_logs(self.root / 'empty.csv')

    def test_parse_contexts_do_not_merge_instances_or_current_databases(self):
        event = get_log_format().to_event(
            self.legacy_row('statement: SELECT 1', '2026-10-01 00:00:00'), 1)
        events = [event, replace(event, source_record=2, instance_id='other'),
                  replace(event, source_record=3, current_database='other'),
                  replace(event, source_record=4, query_id='different-execution')]
        state = LogProfileState({}, COLUMNS)
        new, occurrences = state.add_batch(events)
        self.assertEqual(len(new), 3)
        self.assertEqual(len(occurrences), 4)
        self.assertEqual(state.profile()['sqls'][0]['count'], 2)

    def test_mysql_legacy_projection_and_cross_table_occurrences(self):
        columns = ['original{}'.format(i) for i in range(33)]
        config = dict(host='localhost', user='test', database='logs', table=['day1', 'day2'],
                      columns=columns, batch_rows=1)
        self.assertEqual(mysql_queries(config)[0][1],
                         'SELECT ' + ', '.join('`' + col + '`' for col in columns)
                         + ' FROM `logs`.`day1`')
        row = self.legacy_row('statement: SELECT 1', '2026-10-01 00:00:00')

        class Cursor:
            def execute(self, query):
                self.description = [(col,) for col in columns]
                self.remaining = [row]

            def fetchmany(self, count):
                rows, self.remaining = self.remaining, []
                return rows

        class Connection:
            closed = False

            def cursor(self):
                return Cursor()

            def close(self):
                self.closed = True

        connection = Connection()
        with contextlib.redirect_stdout(io.StringIO()):
            batches = list(iter_mysql_event_batches(config, self.root,
                           connect=lambda **kwargs: connection, password=''))
        self.assertTrue(connection.closed)
        events = [event for batch in batches for event in batch]
        self.assertEqual([event.source_record for event in events], [1, 2])
        self.assertEqual([event.sql_text for event in events], ['SELECT 1', 'SELECT 1'])
        self.assertEqual([event.source_width for event in events], [33, 33])
        row[0] = None
        invalid_connection = Connection()
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(ValueError, '日志时间为空'):
            list(iter_mysql_event_batches(config, self.root,
                 connect=lambda **kwargs: invalid_connection, password=''))
        self.assertTrue(invalid_connection.closed)

    def test_profile_state_does_not_assume_a_source_layout(self):
        state = LogProfileState({})
        self.assertEqual(state.profile()['header'], [])
        event = get_log_format().to_event(
            self.legacy_row('SELECT 1', '2026-10-01 00:00:00'), 1)
        state.add_batch([event])
        self.assertEqual(state.profile()['widths'], {33: 1})


if __name__ == '__main__':
    unittest.main()
