"""End-to-end regressions for partition inference, beyond helper unit tests."""

import _support
import unittest
import test_partition_config as pipeline_tests


class SemanticRegressions(unittest.TestCase):
    def results(self, statement, names=('s.events', 's.events2', 's.events3')):
        helper = pipeline_tests.PartitionConfigurationTests()
        analysis, groups = helper.run_pipeline(
            {name: {'partition_columns': ['data_dt']} for name in names},
            [helper.record(statement)], non_partitioned_tables=['s.other'])
        return analysis, groups['results']

    def assert_unknown(self, statement, table='s.events'):
        analysis, groups = self.results(statement)
        rows = [r for r in groups if r['table'] == table]
        self.assertTrue(rows, (statement, groups))
        self.assertFalse(any(r['partition'].startswith('data_dt=') for r in rows), rows)
        return analysis, rows

    def test_correlated_not_exists_does_not_narrow_outer_table(self):
        self.assert_unknown("SELECT * FROM s.events e WHERE NOT EXISTS "
                            "(SELECT 1 FROM s.other o WHERE e.data_dt=DATE '2026-09-01')")

    def test_outer_join_preserved_side_is_not_filtered(self):
        for join in ('LEFT', 'FULL'):
            with self.subTest(join=join):
                self.assert_unknown(f"SELECT * FROM s.events e {join} JOIN s.other o "
                                    "ON e.id=o.id AND e.data_dt=DATE '2026-09-01'")

    def test_outer_join_nullable_side_filter_still_resolves(self):
        _, rows = self.results("SELECT * FROM s.other o LEFT JOIN s.events e "
                               "ON e.id=o.id AND e.data_dt=DATE '2026-09-01'")
        self.assertEqual([r['partition'] for r in rows if r['table']=='s.events'],
                         ['data_dt=2026-09-01'])

    def test_shared_cte_consumer_does_not_filter_all_consumers(self):
        self.assert_unknown("WITH q AS (SELECT id,data_dt FROM s.events) "
                            "SELECT * FROM q a JOIN q b ON a.id=b.id "
                            "WHERE a.data_dt=DATE '2026-09-01'")

    def test_limit_is_a_pushdown_barrier(self):
        self.assert_unknown("SELECT * FROM (SELECT data_dt FROM s.events ORDER BY id LIMIT 10) q "
                            "WHERE q.data_dt=DATE '2026-09-01'")

    def test_nested_union_aliases_follow_output_positions(self):
        _, rows = self.results("SELECT * FROM (SELECT data_dt AS business_key FROM s.events "
                               "UNION ALL SELECT data_dt AS other_name FROM s.events2 "
                               "UNION ALL SELECT data_dt FROM s.events3) q "
                               "WHERE q.business_key=DATE '2026-09-01'")
        self.assertEqual({(r['table'],r['partition']) for r in rows},
                         {(t, 'data_dt=2026-09-01') for t in ('s.events','s.events2','s.events3')})

    def test_union_other_projection_is_not_assigned_to_partition_key(self):
        _, rows = self.results("SELECT * FROM (SELECT data_dt FROM s.events "
                               "UNION ALL SELECT id FROM s.events2) q "
                               "WHERE q.data_dt=DATE '2026-09-01'")
        self.assertEqual([r['partition'] for r in rows if r['table']=='s.events'],
                         ['data_dt=2026-09-01'])
        self.assertFalse(any(r['partition'].startswith('data_dt=') for r in rows if r['table']=='s.events2'))

    def test_insert_star_never_shifts_following_constant_into_partition(self):
        self.assert_unknown("WITH src AS (SELECT 1 AS id,DATE '2026-09-01' AS data_dt) "
                            "INSERT INTO s.events(id,data_dt,created_dt) "
                            "SELECT src.*,DATE '2026-09-02' FROM src")

    def test_alter_modifiers_and_following_statement_survive(self):
        for modifier in ('ONLY', 'IF EXISTS'):
            with self.subTest(modifier=modifier):
                _, rows = self.results(f"ALTER TABLE {modifier} s.events ADD COLUMN note text; "
                                       "SELECT * FROM s.events2 WHERE data_dt=DATE '2026-09-01'")
                self.assertEqual({r['table'] for r in rows}, {'s.events','s.events2'})
                self.assertIn('data_dt=2026-09-01', [r['partition'] for r in rows if r['table']=='s.events2'])


if __name__ == '__main__':
    unittest.main()
