"""Partition candidate aggregation, independent of actual SQL identifiers."""

import _support
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import test_partition_config as configuration_tests



class PartitionAggregationSemanticsTests(unittest.TestCase):
    run_pipeline = configuration_tests.PartitionConfigurationTests.run_pipeline
    record = staticmethod(configuration_tests.PartitionConfigurationTests.record)

    def rows(self, aggregate, table='s.events', role='查询读取'):
        return {r['partition']: r for r in aggregate['results']
                if r['table'] == table and r['role'] == role}

    def run_aggregate(self, predicates, role='读取', columns=('data_dt',)):
        """Exercise the explicit safe/unsafe predicate interface in isolation."""
        with tempfile.TemporaryDirectory(prefix='partition-aggregate-test-') as tmp:
            folder = Path(tmp)
            profile = {'sqls': [{'id': 'SQL000001', 'count': 3,
                                'first_time': '2026-10-01 00:00:00',
                                'last_time': '2026-10-01 00:00:20'}]}
            conditions = [p['condition'] for p in predicates]
            detail = {'id': 'SQL000001', 'occurrence': 1, 'table': 's.events',
                      'role': role, 'category': '显式schema对象',
                      'all_conditions': 'WHERE: ' + ' AND '.join(conditions),
                      'candidates': '\n'.join(conditions), 'status': ''}
            items = [{'id': 'SQL000001', 'occurrence': 1,
                      'field': 'data_dt', 'origin': 'WHERE',
                      'logic': 'AND条件', **item} for item in predicates]
            analysis = {'details': [detail], 'predicates': items,
                        'index': [{'id': 'SQL000001', 'status': '已解析',
                                   'tables': ['s.events']}],
                        'unresolved': [], 'metrics': {}}
            config = {'version': 1, 'tables': {'s.events': {
                'partition_columns': list(columns)}}, 'non_partitioned_tables': []}
            for name, data in [('profile', profile), ('analysis', analysis),
                               ('partition_config', config)]:
                (folder / (name + '.json')).write_text(json.dumps(data), encoding='utf-8')
            env = _support.subprocess_env( SQL_ANALYSIS_OUTPUT_DIR=str(folder),
                       SQL_ANALYSIS_PARTITION_CONFIG=str(folder / 'partition_config.json'))
            result = subprocess.run(_support.stage_command('aggregate'), env=env,
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return json.loads((folder / 'aggregate.json').read_text())

    def test_known_date_merges_despite_additional_join_expression(self):
        _, aggregate = self.run_pipeline(
            {'s.events': {'partition_columns': ['data_dt']}}, [
                self.record("SELECT * FROM s.events WHERE data_dt=DATE '2026-09-01'", 3),
                self.record("SELECT * FROM s.events a JOIN s.other b ON a.data_dt=b.data_dt "
                            "WHERE a.data_dt=DATE '2026-09-01'", 2,
                            first='2026-10-01 00:00:30', last='2026-10-01 00:00:40'),
            ], non_partitioned_tables=['s.other'])
        rows = self.rows(aggregate)
        self.assertEqual(set(rows), {'data_dt=2026-09-01'})
        self.assertEqual(rows['data_dt=2026-09-01']['count'], 5)
        self.assertEqual(rows['data_dt=2026-09-01']['mean_seconds'], 10)
        self.assertIn('a.data_dt = b.data_dt', rows['data_dt=2026-09-01']['evidence'])

    def test_in_candidates_survive_additional_join_expression(self):
        _, aggregate = self.run_pipeline(
            {'s.events': {'partition_columns': ['data_dt']}}, [self.record(
                "SELECT * FROM s.events a JOIN s.other b ON a.data_dt=b.data_dt "
                "WHERE a.data_dt IN (DATE '2026-09-01',DATE '2026-09-02')", 4)],
            non_partitioned_tables=['s.other'])
        rows = self.rows(aggregate)
        self.assertEqual(set(rows), {'data_dt=2026-09-01', 'data_dt=2026-09-02'})
        self.assertEqual({r['count'] for r in rows.values()}, {4})

    def test_unsafe_equality_neither_intersects_nor_creates_known_values(self):
        aggregate = self.run_aggregate([
            {'condition': "data_dt = '2026-09-01'"},
            {'condition': "data_dt = '2026-09-02'", 'safe_for_partition': False,
             'origin': 'JOIN ON'},
        ])
        row = self.rows(aggregate)['data_dt=2026-09-01']
        self.assertIn('2026-09-02', row['evidence'])
        self.assertIn('未用于确定取值', row['basis'])
        aggregate = self.run_aggregate([
            {'condition': "data_dt = '2026-09-02'", 'safe_for_partition': False},
        ])
        rows = self.rows(aggregate)
        self.assertNotIn('data_dt=2026-09-02', rows)
        self.assertTrue(all('未确定' in key for key in rows))

    def test_eq_in_intersection_and_static_ranges_do_not_invent_values(self):
        _, aggregate = self.run_pipeline(
            {'s.events': {'partition_columns': ['data_dt']}}, [self.record(
                "SELECT * FROM s.events WHERE data_dt IN (DATE '2026-09-01',DATE '2026-09-02',DATE '2026-09-03') "
                "AND data_dt >= DATE '2026-09-02' AND data_dt < DATE '2026-09-03'")])
        self.assertEqual(set(self.rows(aggregate)), {'data_dt=2026-09-02'})
        for condition in [
                "data_dt='2026-09-01' AND data_dt > '2026-09-02'",
                "data_dt='2026-09-01' AND data_dt='2026-09-02'",
                "data_dt IN ('2026-09-01') AND data_dt IN ('2026-09-02')"]:
            with self.subTest(condition=condition):
                _, aggregate = self.run_pipeline(
                    {'s.events': {'partition_columns': ['data_dt']}},
                    [self.record('SELECT * FROM s.events WHERE ' + condition)])
                rows = self.rows(aggregate)
                self.assertEqual(len(rows), 1)
                self.assertTrue(next(iter(rows)).startswith('条件矛盾'))

    def test_string_literal_commas_and_escaped_quotes_are_one_in_value(self):
        _, aggregate = self.run_pipeline(
            {'s.events': {'partition_columns': ['region']}}, [self.record(
                "SELECT * FROM s.events WHERE region IN ('east,west','O''Reilly','east,west')")])
        self.assertEqual(set(self.rows(aggregate)), {'region=east,west', "region=O'Reilly"})

    def test_composite_candidate_identity_preserves_literal_boundaries(self):
        from sql_partition_analysis.domain.access import AccessTarget
        _, aggregate = self.run_pipeline(
            {'s.events': {'partition_columns': ['a', 'b']}}, [
                self.record("SELECT * FROM s.events WHERE a='x AND b=y' AND b='z'"),
                self.record("SELECT * FROM s.events WHERE a='x' AND b='y AND b=z'"),
            ])
        rows = aggregate['results']
        self.assertEqual(len(rows), 2)
        self.assertEqual({row['count'] for row in rows}, {3})
        self.assertEqual({row['partition'] for row in rows}, {"a='x AND b=y' AND b=z", "a=x AND b='y AND b=z'"})
        targets = [AccessTarget(row['table'], 'read', row['partition'], ('a', 'b'),
                                tuple(row['partition_identity'])) for row in rows]
        self.assertEqual(len({target.access_id for target in targets}), 2)

    def test_unevaluated_static_expression_cannot_claim_a_known_key(self):
        _, aggregate = self.run_pipeline(
            {'s.events': {'partition_columns': ['data_dt']}}, [self.record(
                "SELECT * FROM s.events WHERE data_dt=DATE '2026-09-01' "
                "AND CAST(data_dt AS DATE) >= DATE '2026-09-02'")])
        self.assertNotIn('data_dt=2026-09-01', self.rows(aggregate))

    def test_or_not_remain_complete_condition_groups(self):
        for condition in ["data_dt='2026-09-01' OR data_dt='2026-09-02'",
                          "NOT (data_dt='2026-09-01')"]:
            with self.subTest(condition=condition):
                _, aggregate = self.run_pipeline(
                    {'s.events': {'partition_columns': ['data_dt']}},
                    [self.record('SELECT * FROM s.events WHERE ' + condition)])
                self.assertTrue(all(key.startswith('复杂逻辑：') for key in self.rows(aggregate)))

    def test_ctas_assignment_branches_are_alternatives_and_deduplicate(self):
        aggregate = self.run_aggregate([
            {'condition': "data_dt := '2026-09-01'", 'origin': '写入赋值',
             'logic': '写入值，不是筛选条件', 'assignment_branch': 0},
            {'condition': "data_dt := '2026-09-02'", 'origin': '写入赋值',
             'logic': '写入值，不是筛选条件', 'assignment_branch': 1},
            {'condition': "data_dt := '2026-09-01'", 'origin': '写入赋值',
             'logic': '写入值，不是筛选条件', 'assignment_branch': 2},
        ], role='建表写入')
        rows = self.rows(aggregate, role='CTAS写入')
        self.assertEqual(set(rows), {'data_dt=2026-09-01', 'data_dt=2026-09-02'})
        self.assertEqual({r['count'] for r in rows.values()}, {3})
        self.assertEqual(len(aggregate['mapping']), 2)

    def test_update_old_and_new_partition_values_are_separate(self):
        _, aggregate = self.run_pipeline(
            {'s.events': {'partition_columns': ['data_dt']}}, [self.record(
                "UPDATE s.events SET data_dt=DATE '2026-09-02' WHERE data_dt=DATE '2026-09-01'", 5)])
        rows = self.rows(aggregate, role='UPDATE更新')
        self.assertEqual(set(rows), {'data_dt=2026-09-01', 'data_dt=2026-09-02'})
        self.assertEqual({r['count'] for r in rows.values()}, {5})
        self.assertIn('旧分区', rows['data_dt=2026-09-01']['basis'])
        self.assertIn('新分区', rows['data_dt=2026-09-02']['basis'])

    def test_update_same_partition_value_counts_once(self):
        _, aggregate = self.run_pipeline(
            {'s.events': {'partition_columns': ['data_dt']}}, [self.record(
                "UPDATE s.events SET data_dt=DATE '2026-09-01' WHERE data_dt=DATE '2026-09-01'", 5)])
        rows = self.rows(aggregate, role='UPDATE更新')
        self.assertEqual(set(rows), {'data_dt=2026-09-01'})
        self.assertEqual(rows['data_dt=2026-09-01']['count'], 5)
        self.assertEqual(len(aggregate['mapping']), 1)

    def test_update_dynamic_destination_is_retained_with_old_value(self):
        _, aggregate = self.run_pipeline(
            {'s.events': {'partition_columns': ['data_dt']}}, [self.record(
                "UPDATE s.events SET data_dt=CURRENT_DATE WHERE data_dt=DATE '2026-09-01'")])
        rows = self.rows(aggregate, role='UPDATE更新')
        self.assertEqual(set(rows), {'data_dt=2026-09-01', 'data_dt := CURRENT_DATE'})
        self.assertIn('未确定', rows['data_dt := CURRENT_DATE']['basis'])

    def test_update_composite_key_preserves_unchanged_key_values(self):
        _, aggregate = self.run_pipeline(
            {'s.events': {'partition_columns': ['data_dt', 'region']}}, [self.record(
                "UPDATE s.events SET region='west' WHERE data_dt=DATE '2026-09-01' AND region='east'")])
        self.assertEqual(set(self.rows(aggregate, role='UPDATE更新')),
                         {'data_dt=2026-09-01 AND region=east',
                          'data_dt=2026-09-01 AND region=west'})

    def test_update_dynamic_composite_destination_keeps_unchanged_key(self):
        _, aggregate = self.run_pipeline(
            {'s.events': {'partition_columns': ['data_dt', 'region']}}, [self.record(
                "UPDATE s.events SET data_dt=CURRENT_DATE "
                "WHERE data_dt=DATE '2026-09-01' AND region='east'")])
        self.assertEqual(set(self.rows(aggregate, role='UPDATE更新')),
                         {'data_dt=2026-09-01 AND region=east',
                          'region=east AND data_dt := CURRENT_DATE'})


if __name__ == '__main__':
    unittest.main()
