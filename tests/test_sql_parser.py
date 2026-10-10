"""Syntax equivalence at the pinned Python 3.7 SQLGlot boundary."""
import _support
import unittest

from sqlglot import exp
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


if __name__ == '__main__':
    unittest.main()
