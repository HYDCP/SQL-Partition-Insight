"""Engine selection must not silently reinterpret other SQL dialects as ADB."""
import _support
import copy
from dataclasses import replace
import unittest
from unittest.mock import patch

from sql_partition_analysis.analytics.aggregation import aggregate_accesses
from sql_partition_analysis.domain.partition import PartitionCandidate
from sql_partition_analysis.domain.log_event import LogEvent
from sql_partition_analysis.engines import (
    AccessEvidence, EngineAdapter, EngineCapabilities, UnsupportedEngineError,
    engine_capabilities, get_engine_adapter,
)
from sql_partition_analysis.partitioning.analyzer import analyze_profile
from sql_partition_analysis.partitioning.catalog import PartitionCatalog


class EngineAdapterTests(unittest.TestCase):
    def setUp(self):
        self.catalog = PartitionCatalog({
            'version': 1, 'tables': {'s.events': {'partition_columns': ['dt']}}})
        self.profile = {'widths': {'33': 3}, 'sqls': [{
            'id': 'SQL000001', 'sql': "SELECT * FROM s.events WHERE dt=DATE '2026-10-01'",
            'count': 3, 'first_time': '2026-10-01 00:00:00',
            'last_time': '2026-10-01 00:00:20'}]}

    def test_default_adb_aliases_keep_existing_count_and_interval(self):
        original = copy.deepcopy(self.profile)
        analysis = analyze_profile(self.profile, self.catalog)
        expected = aggregate_accesses(self.profile, analysis, self.catalog)
        for name in ('adb_pg', 'adb', 'ADB'):
            with self.subTest(name=name):
                other = analyze_profile(self.profile, self.catalog, engine=name)
                self.assertEqual(other, analysis)
                self.assertEqual(aggregate_accesses(self.profile, other, self.catalog,
                                                   engine=name), expected)
        row, = expected['results']
        self.assertEqual((row['partition'], row['count'], row['mean_seconds']),
                         ('dt=2026-10-01', 3, 10.0))
        self.assertEqual(self.profile, original)

    def test_reserved_and_unknown_engines_fail_before_processing_inputs(self):
        for name in ('doris', 'hive', 'unknown', '', None):
            with self.subTest(name=name):
                with self.assertRaises(UnsupportedEngineError):
                    get_engine_adapter(name)
                with self.assertRaises(UnsupportedEngineError):
                    analyze_profile(None, None, engine=name)
                with self.assertRaises(UnsupportedEngineError):
                    aggregate_accesses(None, None, None, engine=name)
        caps = engine_capabilities()
        self.assertTrue(caps['adb_pg'].partition_evidence)
        self.assertFalse(caps['doris'].analysis)
        self.assertFalse(caps['hive'].analysis)

    def test_event_validation_rejects_foreign_engines_and_unsupported_context(self):
        adapter = get_engine_adapter()
        event = LogEvent(sql_text='SELECT 1', raw_sql='SELECT 1',
                         event_time='2026-10-01 00:00:00', user_name='u',
                         statement_type='', source_record=1)
        for engine in ('adb_pg', 'adb', 'ADB'):
            adapter.validate_event(replace(event, engine_type=engine))
        for engine in ('doris', 'hive', '', None):
            with self.subTest(engine=engine):
                with self.assertRaisesRegex(UnsupportedEngineError, '不匹配'):
                    adapter.validate_event(replace(event, engine_type=engine))
        for field in ('instance_id', 'current_database', 'current_schema',
                      'session_id', 'query_id', 'execution_status'):
            with self.subTest(field=field):
                with self.assertRaisesRegex(UnsupportedEngineError, field):
                    adapter.validate_event(replace(event, **{field: 'unsupported'}))

    def test_common_aggregation_consumes_foreign_evidence_without_pg_parsing(self):
        analysis = analyze_profile(self.profile, self.catalog)
        original = copy.deepcopy(analysis)

        class ExampleAdapter(EngineAdapter):
            name = 'example'
            capabilities = EngineCapabilities(analysis=True, partition_evidence=True)

            def analyze_profile(self, profile, catalog):
                return analysis

            def resolve_access_evidence(self, analyzed, catalog):
                yield AccessEvidence(analyzed['details'][0], (
                    PartitionCandidate('查询读取', 'partition:p1', 'external engine',
                                       ('explicit_name', 'p1')),))

        adapter = ExampleAdapter()
        with patch('sql_partition_analysis.partitioning.parser.parse_one_postgres',
                   side_effect=AssertionError('Common aggregation parsed PostgreSQL')):
            result = aggregate_accesses(self.profile, analysis, self.catalog, engine=adapter)
        row, = result['results']
        self.assertEqual(row['partition'], 'partition:p1')
        self.assertEqual(row['count'], 3)
        self.assertEqual(row['mean_seconds'], 10.0)
        self.assertEqual(result['metrics']['detail_rows'], 1)
        self.assertEqual(analysis, original)


if __name__ == '__main__':
    unittest.main()
