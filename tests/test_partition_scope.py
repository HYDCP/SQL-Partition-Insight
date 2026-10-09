"""Filter lineage tests independent from the pipeline output files."""

import _support

import unittest

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
from sqlglot.optimizer.scope import traverse_scope

from sql_partition_analysis.partitioning.scope import PartitionResolver


class PartitionScopeTests(unittest.TestCase):
    def setup_query(self, sql):
        tree = normalize_identifiers(sqlglot.parse_one(sql, read="postgres"), dialect="postgres")
        scopes = list(traverse_scope(tree))
        cte_refs = {id(table) for scope in scopes for table in scope.tables
                    if isinstance(scope.sources.get(table.alias_or_name), type(scope))}
        tables = [table for table in tree.find_all(exp.Table) if id(table) not in cte_refs]
        resolver = PartitionResolver(scopes, {id(table): {} for table in tables})
        return tree, scopes, tables, resolver

    def resolve_where(self, sql, column_name=None):
        tree, scopes, tables, resolver = self.setup_query(sql)
        where = tree.find(exp.Where)
        column = next(c for c in where.find_all(exp.Column) if column_name is None or c.name == column_name)
        owner = column.find_ancestor(exp.Select)
        scope = next(scope for scope in scopes if scope.expression is owner)
        result = resolver.resolve(scope, column)
        return [(table.db + "." + table.name, mapped.name, derived)
                for table, mapped, derived in result], resolver.reason(scope, column)

    def test_cte_and_derived_column_lists(self):
        for sql in (
            "WITH q(d) AS (SELECT data_dt FROM s.events) SELECT * FROM q WHERE d='2026-09-01'",
            "SELECT * FROM (SELECT data_dt FROM s.events) q(d) WHERE d='2026-09-01'",
        ):
            with self.subTest(sql=sql):
                result, reason = self.resolve_where(sql)
                self.assertEqual(result, [("s.events", "data_dt", True)])
                self.assertEqual(reason, "")

    def test_unqualified_multi_source_stays_unknown(self):
        result, reason = self.resolve_where(
            "SELECT * FROM s.events e JOIN s.other o ON e.id=o.id WHERE data_dt='2026-09-01'")
        self.assertEqual(result, [])
        self.assertIn("归属不明确", reason)

    def test_correlated_or_and_not_do_not_escape_their_scope(self):
        for outer in ("NOT EXISTS", "e.id=1 OR EXISTS", "EXISTS"):
            with self.subTest(outer=outer):
                tree, scopes, tables, resolver = self.setup_query(
                    "SELECT * FROM s.events e WHERE " + outer +
                    " (SELECT 1 FROM s.other o WHERE e.data_dt='2026-09-01')")
                column = next(c for c in tree.find_all(exp.Column) if c.name == "data_dt")
                scope = next(sc for sc in scopes if sc.expression is column.find_ancestor(exp.Select))
                self.assertEqual(resolver.resolve(scope, column), [])
                self.assertIn("相关子查询", resolver.reason(scope, column))

    def test_outer_join_only_nullable_side_on_can_filter(self):
        for side, allowed in (("LEFT", "b"), ("RIGHT", "a"), ("FULL", None), ("INNER", "both")):
            with self.subTest(side=side):
                tree, scopes, tables, resolver = self.setup_query(
                    "SELECT * FROM s.a a " + side + " JOIN s.b b ON a.data_dt=b.data_dt")
                join = tree.find(exp.Join)
                for table in tables:
                    expected = allowed == "both" or table.alias_or_name == allowed
                    self.assertEqual(resolver.safe_on(join, scopes[-1], table), expected)
                    self.assertEqual(bool(resolver.on_reason(join, scopes[-1], table)), not expected)

    def test_left_join_derived_nullable_source_stays_safe(self):
        tree, scopes, tables, resolver = self.setup_query(
            "SELECT * FROM s.a a LEFT JOIN (SELECT data_dt FROM s.b) b ON b.data_dt='2026-09-01'")
        join = tree.find(exp.Join)
        table = next(t for t in tables if t.name == "b")
        self.assertTrue(resolver.safe_on(join, scopes[-1], table))

    def test_shared_cte_keeps_its_own_filter(self):
        tree, scopes, tables, resolver = self.setup_query(
            "WITH events AS (SELECT id,data_dt FROM s.events WHERE data_dt='2026-09-01') "
            "SELECT * FROM events a JOIN events b ON a.id=b.id")
        scope = next(sc for sc in scopes if sc.expression.find(exp.Where))
        column = scope.expression.find(exp.Where).find(exp.Column)
        result = resolver.resolve(scope, column)
        self.assertEqual([(table.name, mapped.name, derived) for table, mapped, derived in result],
                         [("events", "data_dt", False)])

    def test_pushdown_barriers(self):
        queries = (
            ("SELECT data_dt FROM s.events ORDER BY id LIMIT 10", "LIMIT/OFFSET"),
            ("SELECT data_dt FROM s.events OFFSET 10", "LIMIT/OFFSET"),
            ("SELECT data_dt,row_number() OVER(ORDER BY id) AS rn FROM s.events", "窗口"),
            ("SELECT DISTINCT ON(id) data_dt FROM s.events ORDER BY id", "DISTINCT ON"),
        )
        for inner, marker in queries:
            with self.subTest(inner=inner):
                result, reason = self.resolve_where(
                    "SELECT * FROM (" + inner + ") q WHERE q.data_dt='2026-09-01'")
                self.assertEqual(result, [])
                self.assertIn(marker, reason)

    def test_materialized_cte(self):
        result, reason = self.resolve_where(
            "WITH q AS MATERIALIZED (SELECT data_dt FROM s.events) "
            "SELECT * FROM q WHERE data_dt='2026-09-01'")
        self.assertEqual(result, [])
        self.assertIn("MATERIALIZED", reason)

    def test_order_and_plain_distinct_can_pass_direct_field(self):
        for inner in ("SELECT data_dt FROM s.events ORDER BY id", "SELECT DISTINCT data_dt FROM s.events"):
            with self.subTest(inner=inner):
                result, reason = self.resolve_where(
                    "SELECT * FROM (" + inner + ") q WHERE q.data_dt='2026-09-01'")
                self.assertEqual(result, [("s.events", "data_dt", True)])
                self.assertEqual(reason, "")

    def test_union_names_follow_position_recursively(self):
        result, reason = self.resolve_where(
            "SELECT * FROM (SELECT data_dt AS d FROM s.a UNION ALL "
            "SELECT row_date AS other_name FROM s.b UNION ALL SELECT as_of_date FROM s.c) q "
            "WHERE q.d='2026-09-01'")
        self.assertEqual(result, [("s.a", "data_dt", True), ("s.b", "row_date", True),
                                  ("s.c", "as_of_date", True)])
        self.assertEqual(reason, "")

    def test_parenthesized_union_branches(self):
        result, reason = self.resolve_where(
            "SELECT * FROM ((SELECT data_dt AS d FROM s.a) UNION ALL "
            "(SELECT row_date AS x FROM s.b UNION ALL SELECT as_of_date AS y FROM s.c)) q "
            "WHERE q.d='2026-09-01'")
        self.assertEqual(result, [("s.a", "data_dt", True), ("s.b", "row_date", True),
                                  ("s.c", "as_of_date", True)])
        self.assertEqual(reason, "")

    def test_recursive_cte_stays_unknown(self):
        result, reason = self.resolve_where(
            "WITH RECURSIVE q AS (SELECT data_dt FROM s.a UNION ALL SELECT data_dt FROM q) "
            "SELECT * FROM q WHERE data_dt='2026-09-01'")
        self.assertEqual(result, [])
        self.assertIn("递归CTE", reason)

    def test_union_limit_blocks_all_branches(self):
        result, reason = self.resolve_where(
            "SELECT * FROM (SELECT data_dt FROM s.a UNION ALL SELECT row_date FROM s.b LIMIT 10) q "
            "WHERE q.data_dt='2026-09-01'")
        self.assertEqual(result, [])
        self.assertIn("LIMIT/OFFSET", reason)

    def test_nested_nullable_join_group(self):
        tree, scopes, tables, resolver = self.setup_query(
            "SELECT * FROM s.a a LEFT JOIN (s.b b JOIN s.c c ON b.id=c.id) "
            "ON b.data_dt='2026-09-01'")
        join = tree.find(exp.Join)
        self.assertEqual({table.name: resolver.safe_on(join, scopes[-1], table) for table in tables},
                         {"a": False, "b": True, "c": True})

    def test_union_computed_branch_stays_unknown_with_reason(self):
        result, reason = self.resolve_where(
            "SELECT * FROM (SELECT data_dt FROM s.a UNION ALL "
            "SELECT data_dt + INTERVAL '1 DAY' FROM s.b) q WHERE q.data_dt='2026-09-01'")
        self.assertEqual(result, [("s.a", "data_dt", True)])
        self.assertIn("计算列", reason)

    def test_union_star_without_metadata_stays_unknown(self):
        result, reason = self.resolve_where(
            "SELECT * FROM (SELECT * FROM s.a UNION ALL SELECT * FROM s.b) q WHERE q.data_dt='2026-09-01'")
        self.assertEqual(result, [])
        self.assertIn("星号", reason)

    def test_simple_and_qualified_star_passthrough(self):
        for inner in ("SELECT * FROM s.events", "SELECT e.* FROM s.events e JOIN s.other o ON e.id=o.id"):
            with self.subTest(inner=inner):
                result, reason = self.resolve_where(
                    "SELECT * FROM (" + inner + ") q WHERE q.data_dt='2026-09-01'")
                self.assertEqual(result, [("s.events", "data_dt", True)])
                self.assertEqual(reason, "")


if __name__ == "__main__":
    unittest.main()
