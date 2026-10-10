"""Qualified intervals must not become incorrect definite partition values."""
import _support
import unittest
from sqlglot import exp
from sql_partition_analysis.partitioning.parser import parse_one_postgres

import test_partition_analysis_fixes as pipeline_tests


class IntervalFieldRegressionTests(unittest.TestCase):
    def test_qualified_and_unqualified_intervals_have_distinct_partitions(self):
        cases = [("INTERVAL '0.5' YEAR", '2026-01-01'),
                 ("INTERVAL '0.5 years'", '2026-07-01'),
                 ("INTERVAL '-1.5' YEAR", '2025-01-01'),
                 ("INTERVAL '-1.5 years'", '2024-07-01')]
        helper = pipeline_tests.PartitionAnalysisFixesTests()
        for interval, expected in cases:
            with self.subTest(interval=interval):
                sql = ("SELECT * FROM audit.events WHERE data_dt="
                       "CAST(DATE '2026-01-01' + {} AS DATE)").format(interval)
                analysis, report = helper.analyze(sql)
                self.assertEqual(analysis['index'][0]['status'], '已解析')
                self.assertEqual(report['mapping'][0]['partition_identity'],
                                 ('values', (('data_dt', 'date', expected),)))

    def test_day_field_drops_time_in_select_insert_and_update(self):
        helper = pipeline_tests.PartitionAnalysisFixesTests()
        for interval, expected in [("INTERVAL '1.5' DAY", '2026-01-02 00:00:00'),
                                   ("INTERVAL '1.5 days'", '2026-01-02 12:00:00')]:
            expression = "TIMESTAMP '2026-01-01 00:00:00' + " + interval
            for sql in ("SELECT * FROM audit.events WHERE data_dt=" + expression,
                        "INSERT INTO audit.events(data_dt) VALUES (" + expression + ")",
                        "UPDATE audit.events SET data_dt=" + expression + " WHERE id=1"):
                with self.subTest(sql=sql):
                    _, report = helper.analyze(sql)
                    values = {row['partition_identity'] for row in report['mapping']
                              if row['partition_identity'][0] == 'values'}
                    self.assertEqual(values, {('values', (('data_dt', 'timestamp', expected),))})

    def test_other_string_forms_never_lose_the_field_during_reparsing(self):
        helper = pipeline_tests.PartitionAnalysisFixesTests()
        for literal in ("E'1.5'", "$$1.5$$", "$tag$1.5$tag$", "N'1.5'"):
            with self.subTest(literal=literal):
                _, report = helper.analyze(
                    "SELECT * FROM audit.events WHERE data_dt="
                    "TIMESTAMP '2026-01-01 00:00:00' + INTERVAL " + literal + ' DAY')
                self.assertTrue(report['mapping'])
                for row in report['mapping']:
                    if row['partition_identity'][0] == 'values':
                        self.assertEqual(row['partition_identity'],
                                         ('values', (('data_dt', 'timestamp', '2026-01-02 00:00:00'),)))
                    else:
                        self.assertIn('AS INTERVAL DAY', row['partition'])

    def test_interval_precision_remains_unknown_through_the_complete_pipeline(self):
        helper = pipeline_tests.PartitionAnalysisFixesTests()
        for literal in ("'1.5'", "E'1.5'", "$$1.5$$"):
            for fields in ('SECOND(0)', 'SECOND(2)', 'DAY TO SECOND(0)'):
                with self.subTest(literal=literal, fields=fields):
                    interval = 'INTERVAL ' + literal + ' ' + fields
                    node = parse_one_postgres('SELECT ' + interval).expressions[0]
                    rendered = node.sql(dialect='postgres')
                    self.assertIsInstance(node, exp.Cast)
                    self.assertIn('AS INTERVAL ' + fields, rendered)
                    reparsed = parse_one_postgres('SELECT ' + rendered).expressions[0]
                    self.assertEqual(reparsed.args['to'], node.args['to'])
                    self.assertEqual(reparsed.this.this, node.this.this)
                    analysis, report = helper.analyze(
                        "SELECT * FROM audit.events WHERE data_dt="
                        "TIMESTAMP '2026-01-01 00:00:00' + " + interval)
                    self.assertEqual(analysis['index'][0]['status'], '已解析')
                    self.assertTrue(report['mapping'])
                    self.assertTrue(all(row['partition_identity'][0] != 'values'
                                        for row in report['mapping']))
                    self.assertIn('AS INTERVAL ' + fields, report['mapping'][0]['partition'])


if __name__ == '__main__':
    unittest.main()
