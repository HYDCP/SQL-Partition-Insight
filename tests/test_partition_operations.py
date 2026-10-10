"""Regression tests for explicit partition operations and write positions."""

import _support

import unittest

import sqlglot

from sql_partition_analysis.partitioning.operations import describe_partition_action, parse_alter, split_sql_statements, write_assignments


class PartitionOperationsTests(unittest.TestCase):
    def assignments(self, statement):
        return [(field, value.sql(dialect="postgres"), branch)
                for field, value, branch in write_assignments(sqlglot.parse_one(statement, read="postgres"))]

    def test_quoted_table_identifiers_and_partition_name(self):
        table, action = parse_alter('ALTER TABLE ONLY "odd.schema"."Events" ADD PARTITION "p odd" START (DATE \'2026-09-01\') END (DATE \'2026-09-02\');')
        self.assertEqual((table.db, table.name), ("odd.schema", "Events"))
        self.assertEqual(describe_partition_action(action, ["data_dt"]), "分区名=p odd；范围[2026-09-01,2026-09-02)")

    def test_invalid_alter_never_raises(self):
        for statement in ("ALTER TABLE ONLY", 'ALTER TABLE "unterminated', "ALTER TABLE s. ADD COLUMN x int", "SELECT 1"):
            with self.subTest(statement=statement):
                self.assertIsNone(parse_alter(statement))

    def test_statements_do_not_split_inside_quotes_or_comments(self):
        statements = split_sql_statements("ALTER TABLE s.events ADD COLUMN note text DEFAULT 'a;b'; -- ignored ;\n SELECT $$c;d$$;")
        self.assertEqual(len(statements), 2)
        self.assertIn("'a;b'", statements[0])
        self.assertIn("$$c;d$$", statements[1])
        self.assertIsNone(parse_alter("ALTER TABLE s.events ADD COLUMN x int; SELECT 1"))

    def test_inclusive_and_exclusive_boundaries(self):
        action = "ADD PARTITION p START (DATE '2026-09-01') EXCLUSIVE END (DATE '2026-09-02') INCLUSIVE"
        self.assertEqual(describe_partition_action(action, ["data_dt"]), "分区名=p；范围(2026-09-01,2026-09-02]")

    def test_list_values_do_not_become_a_range(self):
        action = "ADD PARTITION p VALUES (DATE '2026-09-01', DATE '2026-09-03')"
        self.assertEqual(describe_partition_action(action, ["data_dt"]), "分区名=p；VALUES(2026-09-01,2026-09-03)")

    def test_range_expression_retains_complete_expression(self):
        action = "ADD PARTITION p START (DATE '2026-09-01' + INTERVAL '1 day') END (DATE '2026-09-03')"
        result = describe_partition_action(action, ["data_dt"])
        self.assertIn("+ INTERVAL", result)
        self.assertNotEqual(result, "分区名=p；范围[2026-09-01,2026-09-03)")

    def test_nonpartition_action_keywords_do_not_trigger_partition_ddl(self):
        for action in ("ADD COLUMN partition text", "ALTER COLUMN partition SET DEFAULT 'partition'", "ADD COLUMN note text DEFAULT 'PARTITION FOR (20260901)'"):
            self.assertIsNone(describe_partition_action(action, ["data_dt"]))

    def test_multiple_actions_preserve_all_target_evidence(self):
        action = "ADD PARTITION p START ('2026-09-01') END ('2026-09-02'), DROP PARTITION q"
        self.assertEqual(describe_partition_action(action, ["data_dt"]), action)

    def test_star_blocks_positional_assignments_for_whole_insert(self):
        statement = "WITH src AS (SELECT 1 AS id, DATE '2026-09-01' AS data_dt) INSERT INTO s.events(id,data_dt,created_dt) SELECT src.*, DATE '2026-09-02' FROM src"
        self.assertEqual(self.assignments(statement), [])
        self.assertEqual(self.assignments("INSERT INTO s.events(data_dt,id) SELECT * FROM s.src"), [])
        self.assertEqual(self.assignments("INSERT INTO s.events(data_dt) SELECT DATE '2026-09-01' UNION ALL SELECT * FROM s.src"), [])

    def test_select_count_star_remains_a_single_projection(self):
        result = self.assignments("INSERT INTO s.events(data_dt,n) SELECT DATE '2026-09-01', COUNT(*) FROM s.src")
        self.assertEqual([field for field, _, _ in result], ["data_dt", "n"])

    def test_except_and_mismatched_positions_are_unknown(self):
        self.assertEqual(self.assignments("INSERT INTO s.events(data_dt) SELECT DATE '2026-09-01' EXCEPT SELECT DATE '2026-09-02'"), [])
        self.assertEqual(self.assignments("INSERT INTO s.events(id,data_dt) SELECT DATE '2026-09-01'"), [])
        self.assertEqual(self.assignments("INSERT INTO s.events SELECT DATE '2026-09-01'"), [])

    def test_ctas_columns_are_defined_by_list_or_projection(self):
        for statement in ("CREATE TABLE s.events(data_dt) AS SELECT DATE '2026-09-01'", "CREATE TABLE s.events AS SELECT DATE '2026-09-01' AS data_dt"):
            self.assertEqual([field for field, _, _ in self.assignments(statement)], ["data_dt"])

    def test_update_tuple_single_row_subquery_retains_target_positions(self):
        expected = [("data_dt", "2", 0), ("id", "9", 0)]
        for values in ("(SELECT 2, 9)", "((SELECT 2 AS dt, 9 AS row_id))",
                       "(VALUES (2, 9))"):
            with self.subTest(values=values):
                self.assertEqual(self.assignments(
                    "UPDATE s.events SET (data_dt,id)=" + values + " WHERE data_dt=1"), expected)
        self.assertEqual(self.assignments(
            "UPDATE s.events SET (data_dt,id)=(SELECT DATE '2026-10-08',9)"),
            [("data_dt", "CAST('2026-10-08' AS DATE)", 0), ("id", "9", 0)])

    def test_update_tuple_subqueries_with_unproved_cardinality_stay_unknown(self):
        unknown = (
            "(SELECT 2)", "(SELECT 2,9,10)", "(SELECT 2,9 FROM s.src)",
            "(SELECT 2,9 WHERE FALSE)", "(SELECT 2,9 HAVING FALSE)",
            "(SELECT 2,9 GROUP BY 1,2)", "(SELECT 2,9 LIMIT 0)",
            "(SELECT 2,9 OFFSET 1)", "(WITH c AS (SELECT 1) SELECT 2,9)",
            "(SELECT 2,9 UNION ALL SELECT 3,10)", "(VALUES (2,9),(3,10))",
            "(SELECT generate_series(2,3),9)", "(SELECT COUNT(*),9)",
            "(SELECT SUM(2) OVER (),9)", "(SELECT (SELECT 2),9)",
            "(SELECT src.data_dt,9)", "(SELECT CAST('bad' AS INTEGER),9)",
        )
        for values in unknown:
            with self.subTest(values=values):
                self.assertEqual(self.assignments(
                    "UPDATE s.events SET (data_dt,id)=" + values + " WHERE data_dt=1"), [])

    def test_update_single_row_subquery_produces_old_and_new_partition_evidence(self):
        from sql_partition_analysis.partitioning.analyzer import analyze_profile
        from sql_partition_analysis.partitioning.catalog import PartitionCatalog
        from sql_partition_analysis.analytics.aggregation import aggregate_accesses

        catalog = PartitionCatalog({'version': 1, 'tables': {
            's.events': {'partition_columns': ['data_dt'], 'partition_type': 'LIST'}}})
        for values in ("(SELECT 2,9)", "(VALUES (2,9))"):
            with self.subTest(values=values):
                profile = {'widths': {'fixture': 1}, 'sqls': [{
                    'id': 'Q1', 'sql': 'UPDATE s.events SET (data_dt,id)=' + values + ' WHERE data_dt=1',
                    'count': 1, 'first_time': '2026-10-10 00:00:00',
                    'last_time': '2026-10-10 00:00:00', 'user_name': 'audit'}]}
                analysis = analyze_profile(profile, catalog)
                report = aggregate_accesses(profile, analysis, catalog)
                self.assertEqual({(row['role'], row['partition_identity']) for row in report['mapping']}, {
                    ('UPDATE更新', ('values', (('data_dt', 'number', '1'),))),
                    ('UPDATE更新', ('values', (('data_dt', 'number', '2'),)))})
                self.assertTrue(any('UPDATE旧分区' in row['basis'] for row in report['mapping']))
                self.assertTrue(any('UPDATE新分区' in row['basis'] for row in report['mapping']))


if __name__ == "__main__":
    unittest.main()
