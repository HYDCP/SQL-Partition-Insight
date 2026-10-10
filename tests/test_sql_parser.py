"""Syntax equivalence at the pinned Python 3.7 SQLGlot boundary."""
import _support
import unittest

from sqlglot import exp
from sqlglot.errors import ParseError
from sql_partition_analysis.partitioning.parser import compatible_postgres, parse_one_postgres
import test_partition_config as pipeline_tests


class ParserCompatibilityTests(unittest.TestCase):
    def test_signed_intervals_keep_their_value(self):
        for value, unit in [('-1', 'MONTH'), ('-5', 'DAY'), ('1', 'YEAR'), ('-1.5', 'HOUR')]:
            with self.subTest(value=value, unit=unit):
                node = parse_one_postgres("SELECT INTERVAL '{} {}'".format(value, unit)).find(exp.Interval)
                self.assertEqual(node.this.this, value)
                self.assertEqual(node.args['unit'].this, unit)

    def test_strings_comments_and_dollar_quotes_are_not_rewritten(self):
        text = "SELECT $$INTERVAL '-1 MONTH'$$, 'DATE ''20260727''::DATE' -- INTERVAL '-2 DAY'\n"
        self.assertEqual(compatible_postgres(text), text)

    def test_negative_month_arithmetic_in_complete_pipeline(self):
        helper = pipeline_tests.PartitionConfigurationTests()
        analysis, result = helper.run_pipeline({'s.events': {'partition_columns': ['data_dt']}}, [helper.record(
            "SELECT * FROM s.events WHERE data_dt = DATE '2026-06-30' + INTERVAL '1 DAY' "
            "+ INTERVAL '-1 MONTH' + INTERVAL '-1 DAY'")])
        # PostgreSQL DATE + INTERVAL returns TIMESTAMP; preserve its time/type.
        self.assertEqual(result['results'][0]['partition'], 'data_dt=2026-05-31 00:00:00')
        self.assertIn('AS TIMESTAMP', analysis['predicates'][0]['condition'])
        self.assertEqual(result['results'][0]['count'], 3)

    def test_projection_cast_does_not_drop_table_access(self):
        helper = pipeline_tests.PartitionConfigurationTests()
        _, result = helper.run_pipeline({'s.events': {'partition_columns': ['data_dt']}}, [helper.record(
            "INSERT INTO s.target(data_dt) SELECT DATE'20260727'::DATE FROM s.events "
            "WHERE data_dt=DATE'20260727'")])
        self.assertEqual(result['results'][0]['partition'], 'data_dt=2026-07-27')
        self.assertEqual(result['results'][0]['count'], 3)

    def test_interval_rewrite_keeps_adjacent_suffix_token_boundaries(self):
        cases = ["SELECT CAST(INTERVAL '-1 DAY'as date)",
                 "SELECT INTERVAL '-1 DAY'at time zone 'UTC'",
                 "SELECT INTERVAL '-1 DAY'alias_1",
                 "SELECT INTERVAL '-1 DAY'alias$",
                 "SELECT INTERVAL '-1 DAY'中文",
                 "SELECT INTERVAL '-1 DAY'💥",
                 "SELECT INTERVAL '-1 DAY'\u0301"]
        for sql in cases:
            with self.subTest(sql=sql):
                node = parse_one_postgres(sql)
                interval = node.find(exp.Interval)
                self.assertEqual((interval.this.this, interval.args['unit'].this), ('-1', 'DAY'))
                # The AST must match the same SQL with an explicit separator.
                separated = sql.replace("DAY'", "DAY' ")
                self.assertEqual(node, parse_one_postgres(separated))
                self.assertIn('DAY ', compatible_postgres(sql))

    def test_interval_suffix_punctuation_and_comments_are_preserved(self):
        sql = "SELECT INTERVAL '-1 DAY'/* suffix */+INTERVAL '2 HOURS'"
        node = parse_one_postgres(sql)
        self.assertEqual([interval.this.this for interval in node.find_all(exp.Interval)], ['-1', '2'])
        self.assertIn('/* suffix */+', compatible_postgres(sql))
        self.assertEqual(parse_one_postgres("SELECT (INTERVAL '-1 DAY')"),
                         parse_one_postgres("SELECT (INTERVAL '-1 DAY' )"))

    def test_unreserved_apply_table_alias_matches_explicit_as(self):
        cases = [('SELECT apply.data_dt FROM s.events apply WHERE apply.data_dt=1',
                  'SELECT apply.data_dt FROM s.events AS apply WHERE apply.data_dt=1'),
                 ('SELECT * FROM apply apply WHERE apply.data_dt=1',
                  'SELECT * FROM apply AS apply WHERE apply.data_dt=1'),
                 ('SELECT * FROM (SELECT * FROM s.events) apply',
                  'SELECT * FROM (SELECT * FROM s.events) AS apply'),
                 ('SELECT * FROM (VALUES (1),(2)) apply(data_dt)',
                  'SELECT * FROM (VALUES (1),(2)) AS apply(data_dt)'),
                 ('SELECT * FROM s.events apply JOIN s.target t ON apply.id=t.id',
                  'SELECT * FROM s.events AS apply JOIN s.target t ON apply.id=t.id'),
                 ('UPDATE s.events apply SET data_dt=2 WHERE apply.data_dt=1',
                  'UPDATE s.events AS apply SET data_dt=2 WHERE apply.data_dt=1'),
                 ('DELETE FROM s.events apply WHERE apply.data_dt=1',
                  'DELETE FROM s.events AS apply WHERE apply.data_dt=1')]
        for sql, explicit in cases:
            with self.subTest(sql=sql):
                self.assertEqual(parse_one_postgres(sql), parse_one_postgres(explicit))
        table = parse_one_postgres('SELECT apply FROM apply').args['from'].this
        self.assertEqual(table.name, 'apply')
        self.assertFalse(table.alias)
        # Neither a table alias nor a VALUES keyword can become an Alias with
        # a missing left expression while the statement parser probes syntax.
        for sql in ('VALUES (1),(2)', 'INSERT INTO s.events(data_dt) VALUES (1),(2)',
                    'WITH x(data_dt) AS (VALUES (1)) SELECT * FROM x'):
            tree = parse_one_postgres(sql)
            self.assertTrue(list(tree.find_all(exp.Values)))
            self.assertFalse(any(alias.this is None for alias in tree.find_all(exp.Alias)))
        with self.assertRaises(ParseError):
            parse_one_postgres('SELECT * FROM APPLY s.events')
        with self.assertRaises(ParseError):
            parse_one_postgres('SELECT * FROM')

    def test_apply_joins_still_parse_as_lateral_joins(self):
        for kind, cross in [('CROSS', True), ('OUTER', False)]:
            with self.subTest(kind=kind):
                tree = parse_one_postgres('SELECT * FROM s.events {} APPLY (SELECT 1) x'.format(kind))
                self.assertFalse(tree.args['from'].this.alias)
                lateral = tree.args['joins'][0].this
                self.assertIsInstance(lateral, exp.Lateral)
                self.assertIs(lateral.args['cross_apply'], cross)

    def test_interval_cast_and_apply_alias_recover_complete_partition_accesses(self):
        helper = pipeline_tests.PartitionConfigurationTests()
        for sql, expected in [
                ("SELECT * FROM s.events WHERE data_dt=CAST(DATE '20261007'"
                 "+INTERVAL '1 DAY'as date)", 'data_dt=2026-10-08'),
                ("SELECT * FROM s.events apply WHERE apply.data_dt=DATE '20261007'", 'data_dt=2026-10-07')]:
            with self.subTest(sql=sql):
                analysis, result = helper.run_pipeline({'s.events': {'partition_columns': ['data_dt']}},
                                                      [helper.record(sql)])
                self.assertEqual(analysis['index'][0]['status'], '已解析')
                self.assertEqual(result['results'][0]['partition'], expected)
                self.assertEqual(result['results'][0]['count'], 3)

    def test_interval_recovery_preserves_unproved_quarter_expression(self):
        helper = pipeline_tests.PartitionConfigurationTests()
        expression = ("CAST(DATE_TRUNC('quarter', DATE '20261007')"
                      "+INTERVAL '1 month'-INTERVAL '1 DAY'as date)")
        analysis, result = helper.run_pipeline({'s.events': {'partition_columns': ['data_dt']}},
                                              [helper.record('SELECT * FROM s.events WHERE data_dt=' + expression)])
        self.assertEqual(analysis['index'][0]['status'], '已解析')
        self.assertNotEqual(result['mapping'][0]['partition_identity'][0], 'values')
        self.assertIn("DATE_TRUNC('QUARTER'", result['mapping'][0]['partition'])
        self.assertIn("INTERVAL '1 DAY' AS DATE)", result['mapping'][0]['partition'])


if __name__ == '__main__':
    unittest.main()
