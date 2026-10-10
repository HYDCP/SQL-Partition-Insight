"""Generic regressions for issues found in actual server log evidence."""
import _support
import unittest

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers

from sql_partition_analysis.partitioning.analyzer import analyze_profile
from sql_partition_analysis.partitioning.catalog import PartitionCatalog
from sql_partition_analysis.partitioning.operations import recover_copy_header
from sql_partition_analysis.partitioning.scope import resolve_direct_column
from sql_partition_analysis.analytics.aggregation import aggregate_accesses


class PartitionAnalysisFixesTests(unittest.TestCase):
    def analyze(self, statement, tables=None):
        catalog = PartitionCatalog({'version': 1, 'tables': tables or {
            'audit.events': {'partition_columns': ['data_dt']}}, 'non_partitioned_tables': []})
        profile = {'widths': {'fixture': 1}, 'sqls': [{
            'id': 'SQL000001', 'sql': statement, 'count': 1,
            'first_record': 1, 'user_name': 'audit', 'types': ['LOG'],
            'first_time': '2026-10-01 00:00:00', 'last_time': '2026-10-01 00:00:00'}]}
        analysis = analyze_profile(profile, catalog)
        return analysis, aggregate_accesses(profile, analysis, catalog)

    def test_update_self_join_keeps_target_and_source_dates_separate(self):
        analysis, aggregate = self.analyze(
            "UPDATE audit.events SET payload=p.payload FROM audit.events p "
            "WHERE audit.events.data_dt=DATE '20261002' "
            "AND p.data_dt=DATE '20261001' AND audit.events.id=p.id")
        rows = {(row['role'], row['partition']) for row in aggregate['results']}
        self.assertIn(('UPDATE更新', 'data_dt=2026-10-02'), rows)
        self.assertIn(('查询读取', 'data_dt=2026-10-01'), rows)
        self.assertEqual(analysis['unresolved'], [])

    def test_dml_schema_qualifiers_and_alias_hiding(self):
        tree = normalize_identifiers(sqlglot.parse_one(
            'UPDATE left_schema.events SET x=p.x FROM right_schema.events p', read='postgres'), dialect='postgres')
        tables = list(tree.find_all(exp.Table))
        for reference, expected in [('left_schema.events.data_dt', 'left_schema'),
                                    ('p.data_dt', 'right_schema'),
                                    ('right_schema.events.data_dt', None),
                                    ('missing.events.data_dt', None),
                                    ('data_dt', None)]:
            with self.subTest(reference=reference):
                column = sqlglot.parse_one('SELECT ' + reference, read='postgres').find(exp.Column)
                matches = resolve_direct_column(tables, column)
                self.assertEqual([table.db for table, _, _ in matches], [expected] if expected else [])

    def test_copy_failure_retains_direction_without_claiming_truncation(self):
        for direction, expected in [('FROM', 'COPY写入'), ('TO', 'COPY读取')]:
            result = recover_copy_header(
                '/* COPY fake */ COPY audit.events ("from", "to") ' + direction + " STDIN WITH (unsupported = 'FROM')")
            table, role, incomplete = result
            self.assertEqual((table.db, table.name, role, incomplete), ('audit', 'events', expected, False))

    def test_copy_incomplete_column_list_has_unknown_direction(self):
        for statement in ('COPY audit.events(data_dt,', 'COPY audit.events(data_dt,"unfinished'):
            table, role, incomplete = recover_copy_header(statement)
            self.assertEqual((table.db, table.name, role, incomplete), ('audit', 'events', None, True))
        self.assertIsNone(recover_copy_header('COPY (SELECT * FROM audit.events) TO STDOUT'))
        for statement in ('COPY audit.events."unfinished', 'COPY audit.events.',
                          'COPY audit.events. /* unfinished', 'COPY audit."unfinished'):
            with self.subTest(statement=statement):
                self.assertIsNone(recover_copy_header(statement))

    def test_copy_broken_options_do_not_erase_complete_header(self):
        for direction, expected in [('FROM', 'COPY写入'), ('TO', 'COPY读取')]:
            for suffix in ("WITH DELIMITER 'unterminated", 'WITH (option = $$unterminated',
                           '/* unterminated', "WITH (option = E'unterminated\\'"):
                with self.subTest(direction=direction, suffix=suffix):
                    table, role, incomplete = recover_copy_header(
                        '/* prefix */ COPY audit.events ("from", "to") ' + direction + ' STDIN ' + suffix)
                    self.assertEqual((table.db, table.name, role, incomplete), ('audit', 'events', expected, False))

    def test_copy_empty_identifier_is_rejected_and_encoded_complete_target_retained(self):
        self.assertIsNone(recover_copy_header('COPY "" (data_dt,'))
        table, role, incomplete = recover_copy_header('COPY ""audit"".""events"" (""data_dt')
        self.assertEqual((table.db, table.name, role, incomplete), ('audit', 'events', None, True))
        self.assertTrue(table.meta['identifier_quote_recovery'])
        for relation, expected in [('""audit"".""from""', ('audit', 'from')),
                                   ('""to"".""events""', ('to', 'events')),
                                   ('""audit"".""122_event(日志)""', ('audit', '122_event(日志)')),
                                   ('audit /* relation */ . ""from""', ('audit', 'from'))]:
            with self.subTest(relation=relation):
                table, role, incomplete = recover_copy_header('COPY ' + relation + ' (""data_dt')
                self.assertEqual((table.db, table.name, role, incomplete), (*expected, None, True))
                table, role, incomplete = recover_copy_header('COPY ' + relation + " FROM STDIN WITH DELIMITER 'unterminated")
                self.assertEqual((table.db, table.name, role, incomplete), (*expected, 'COPY写入', False))
        analysis, aggregate = self.analyze(
            'An exception was encountered during the execution of statement: '
            'COPY ""audit"".""events"" (""data_dt')
        self.assertEqual(analysis['details'][0]['table'], 'audit.events')
        self.assertIn('双重引号编码兼容还原', analysis['index'][0]['notes'])
        self.assertEqual(aggregate['results'], [])

    def test_exception_body_keeps_evidence_without_counting_failed_access(self):
        analysis, aggregate = self.analyze(
            "An exception was encountered during the execution of statement: "
            "SELECT * FROM audit.events WHERE data_dt=DATE '20261007'")
        self.assertEqual(analysis['details'][0]['table'], 'audit.events')
        self.assertFalse(analysis['details'][0]['statistics_eligible'])
        self.assertEqual(analysis['predicates'][0]['value'], '2026-10-07')
        self.assertEqual(aggregate['results'], [])
        self.assertEqual(aggregate['metrics']['access_log_count'], 0)

    def test_literal_date_lookalikes_remain_original_values(self):
        for original in ("'20261007'", '20261007'):
            with self.subTest(original=original):
                analysis, aggregate = self.analyze('SELECT * FROM audit.events WHERE data_dt=' + original)
                self.assertEqual(aggregate['results'][0]['partition'], 'data_dt=20261007')
                self.assertIn(original, analysis['predicates'][0]['condition'])

    def test_typed_and_untyped_values_do_not_manufacture_a_contradiction(self):
        _, aggregate = self.analyze(
            "SELECT * FROM audit.events WHERE data_dt=DATE '20261007' AND data_dt='20261007'")
        self.assertNotIn('矛盾', aggregate['results'][0]['partition'])
        self.assertIn('CAST', aggregate['results'][0]['partition'])

    def test_unknown_string_type_and_collation_do_not_prove_a_conflict(self):
        for condition in ("data_dt='20261007' AND data_dt='2026-10-07'",
                          "data_dt='01' AND data_dt='1'",
                          "data_dt IN ('20261007','20261008') AND data_dt IN ('2026-10-07','20261008')",
                          "data_dt='20261007' AND data_dt > '2026-10-10'"):
            with self.subTest(condition=condition):
                _, aggregate = self.analyze('SELECT * FROM audit.events WHERE ' + condition)
                self.assertNotIn('矛盾', aggregate['results'][0]['partition'])
                self.assertIn('未展开', aggregate['results'][0]['basis'])

    def test_encoded_identifiers_are_recovered_with_original_sql_intact(self):
        statement = 'SELECT ""data_dt"" FROM ""audit"".""events"" WHERE ""data_dt""=\'20261007\''
        analysis, aggregate = self.analyze(statement)
        self.assertEqual(aggregate['results'][0]['partition'], 'data_dt=20261007')
        self.assertIn('双重引号编码兼容还原', analysis['index'][0]['notes'])

    def test_encoded_copy_has_known_direction_and_unknown_row_values(self):
        analysis, aggregate = self.analyze('COPY ""audit"".""events"" (""data_dt"") FROM STDIN WITH CSV')
        self.assertEqual(analysis['details'][0]['role'], 'COPY写入')
        self.assertNotIn('截断', analysis['index'][0]['status'])
        self.assertIn('未知', aggregate['results'][0]['partition'])


if __name__ == '__main__':
    unittest.main()
