"""Definite scalar CAST values must survive extraction into access identities."""
import _support
import unittest

from sql_partition_analysis.analytics.aggregation import aggregate_accesses
from sql_partition_analysis.partitioning.analyzer import analyze_profile
from sql_partition_analysis.partitioning.catalog import PartitionCatalog


class StaticCastRegressionTests(unittest.TestCase):
    def analyze(self, sql):
        catalog = PartitionCatalog({'version': 1, 'tables': {
            's.events': {'partition_columns': ['data_dt']}}})
        profile = {'sqls': [{'id': 'Q1', 'sql': sql, 'count': 1,
                            'user_name': 'u', 'first_record': 1, 'types': ['SELECT'],
                            'first_time': '2026-10-09 00:00:00',
                            'last_time': '2026-10-09 00:00:00'}], 'widths': {'33': 1}}
        analysis = analyze_profile(profile, catalog)
        return analysis, aggregate_accesses(profile, analysis, catalog)

    def values(self, report):
        return {tuple(value) for row in report['mapping']
                if row['partition_identity'][0] == 'values'
                for value in row['partition_identity'][1]}

    def test_static_cast_types_are_preserved_through_aggregation(self):
        cases = [("CAST('001' AS INTEGER)", 'number', '1'),
                 ("CAST(1.00 AS INTEGER)", 'number', '1'),
                 ("CAST('1.50' AS NUMERIC)", 'number', '1.5'),
                 ("CAST(1.50 AS TEXT)", 'string', '1.50'),
                 ("CAST(-0.00 AS TEXT)", 'string', '0.00'),
                 ("CAST('a' AS VARCHAR)", 'string', 'a'),
                 ("CAST(CAST('001' AS INTEGER) AS TEXT)", 'string', '1'),
                 ("-CAST('-32767' AS SMALLINT)", 'number', '32767'),
                 ('CAST(32767 AS SMALLINT) + 1', 'number', '32768'),
                 ('CAST(2147483647 AS BIGINT) + 1', 'number', '2147483648'),
                 ('-(-2147483648)', 'number', '2147483648'),
                 ('-(-9223372036854775808)', 'number', '9223372036854775808'),
                 ("-CAST('123456789012345678901234567890.123' AS NUMERIC)",
                  'number', '-123456789012345678901234567890.123')]
        for expression, kind, value in cases:
            with self.subTest(expression=expression):
                analysis, report = self.analyze('SELECT * FROM s.events WHERE data_dt=' + expression)
                self.assertEqual(analysis['predicates'][0]['status'], '静态条件')
                self.assertEqual(self.values(report), {('data_dt', kind, value)})

    def test_cast_in_members_are_distinct_and_keep_text_scale(self):
        _, report = self.analyze(
            "SELECT * FROM s.events WHERE data_dt IN (CAST(1.50 AS TEXT), CAST(1.5 AS TEXT))")
        self.assertEqual(self.values(report), {('data_dt', 'string', '1.50'),
                                             ('data_dt', 'string', '1.5')})
        _, report = self.analyze(
            "SELECT * FROM s.events WHERE data_dt IN (CAST('001' AS INTEGER), CAST('2.0' AS NUMERIC))")
        self.assertEqual(self.values(report), {('data_dt', 'number', '1'),
                                             ('data_dt', 'number', '2')})

    def test_static_cast_insert_and_update_destinations_are_known(self):
        _, report = self.analyze("INSERT INTO s.events(data_dt) SELECT CAST('1.50' AS NUMERIC)")
        self.assertEqual(self.values(report), {('data_dt', 'number', '1.5')})
        self.assertEqual({row['role'] for row in report['mapping']}, {'INSERT写入'})
        _, report = self.analyze("UPDATE s.events SET data_dt=CAST('7' AS INTEGER) WHERE data_dt=6")
        self.assertEqual(self.values(report), {('data_dt', 'number', '6'), ('data_dt', 'number', '7')})
        self.assertEqual({row['role'] for row in report['mapping']}, {'UPDATE更新'})

    def test_uncertain_cast_semantics_stay_as_complete_expressions(self):
        cases = ["CAST(DATE '20261007' AS TEXT)", "CAST('1.55' AS NUMERIC(3,1))",
                 "CAST(1.5 AS INTEGER)", "CAST('2147483648' AS INTEGER)",
                 "-CAST('-32768' AS SMALLINT)", "-CAST('-2147483648' AS INTEGER)",
                 "-CAST('-9223372036854775808' AS BIGINT)",
                 'CAST(32767 AS SMALLINT) + CAST(1 AS SMALLINT)',
                 "CAST('١' AS INTEGER)", "CAST('١.٥' AS NUMERIC)",
                 "CAST('\u00a01' AS INTEGER)", "CAST('\u00a01.5' AS NUMERIC)",
                 "CAST('1e-16384' AS NUMERIC)", "CAST('0e-16384' AS NUMERIC)",
                 "CAST('1e131072' AS NUMERIC)",
                 '1e-16384', '-1e-16384', '1e131072',
                 "CAST('2026-10-07 12:00:00' AS TIMESTAMP(0))"]
        for expression in cases:
            with self.subTest(expression=expression):
                _, report = self.analyze('SELECT * FROM s.events WHERE data_dt=' + expression)
                self.assertFalse(self.values(report))
                self.assertTrue(report['mapping'])
                self.assertTrue(all(row['partition_identity'][0] != 'values'
                                    for row in report['mapping']))


if __name__ == '__main__':
    unittest.main()
