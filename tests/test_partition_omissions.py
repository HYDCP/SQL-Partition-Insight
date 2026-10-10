"""Mutation checks prove the checker does not depend on analyzer output."""
import _support
import copy
import unittest
from unittest.mock import patch

from sql_partition_analysis.validation.partition_omissions import check_partition_omissions


CONFIG = {'version': 1, 'tables': {
    's.events': {'partition_columns': ['data_dt'], 'partition_type': 'RANGE'},
    's.target': {'partition_columns': ['data_dt'], 'partition_type': 'LIST'}}}


def profile(sql, sid='Q1', user='alice', count=3):
    return {'sqls': [{'id': sid, 'sql': sql, 'user_name': user, 'count': count,
                     'first_record': 1, 'types': {'SELECT': count},
                     'first_time': '2026-10-09 01:00:00', 'last_time': '2026-10-09 01:00:02'}],
            'widths': {'33': count}}


def output(values, table='s.events', role='查询读取', occurrence=1, sid='Q1'):
    identity = ('values', tuple(('data_dt', kind, value) for kind, value in values))
    row = {'id': sid, 'table': table, 'occurrence': occurrence, 'role': role,
           'partition_identity': identity, 'raw_partition': 'presentation irrelevant'}
    result = {'table': table, 'role': role, 'partition_identity': identity, 'ids': [sid]}
    return {'partition_config': copy.deepcopy(CONFIG), 'mapping': [row], 'results': [result]}


class PartitionOmissionTests(unittest.TestCase):
    def check(self, sql, report=None):
        return check_partition_omissions(profile(sql), report or {
            'partition_config': CONFIG, 'mapping': [], 'results': []})

    def test_normal_typed_date_and_bare_string_have_distinct_identities(self):
        sql = "SELECT * FROM s.events WHERE data_dt=DATE '20261007'"
        self.assertFalse(self.check(sql, output([('date', '2026-10-07')]))['alerts'])
        wrong = self.check(sql, output([('string', '2026-10-07')]))
        self.assertEqual(wrong['alerts'][0]['partition_value'], {'type': 'date', 'value': '2026-10-07'})
        bare = "SELECT * FROM s.events WHERE data_dt='20261007'"
        self.assertFalse(self.check(bare, output([('string', '20261007')]))['alerts'])
        self.assertTrue(self.check(bare, output([('string', '2026-10-07')]))['alerts'])

    def test_missing_table_mapping_and_missing_statistical_group(self):
        sql = 'SELECT * FROM s.events WHERE data_dt=7'
        report = output([('number', '7')])
        no_mapping = copy.deepcopy(report)
        no_mapping['mapping'] = []
        self.assertEqual(self.check(sql, no_mapping)['alerts'][0]['missing_outputs'], ['mapping'])
        no_result = copy.deepcopy(report)
        no_result['results'] = []
        self.assertEqual(self.check(sql, no_result)['alerts'][0]['missing_outputs'], ['results'])

    def test_missing_one_in_member_is_detected(self):
        sql = "SELECT * FROM s.events WHERE data_dt IN ('a','b','c')"
        report = output([('string', 'a'), ('string', 'c')])
        alerts = self.check(sql, report)['alerts']
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['partition_value'], {'type': 'string', 'value': 'b'})

    def test_wrong_role_and_wrong_occurrence_do_not_satisfy_read(self):
        sql = 'SELECT * FROM s.events WHERE data_dt=1'
        self.assertTrue(self.check(sql, output([('number', '1')], role='INSERT写入'))['alerts'])
        result = self.check(sql, output([('number', '1')], occurrence=2))
        self.assertEqual(result['alerts'][0]['missing_outputs'], ['mapping'])

    def test_same_table_self_join_requires_each_reference(self):
        sql = ('SELECT * FROM s.events a JOIN s.events b ON a.id=b.id '
               'WHERE a.data_dt=1 AND b.data_dt=2')
        a = output([('number', '1')])
        b = output([('number', '2')], occurrence=2)
        report = dict(a, mapping=a['mapping'] + b['mapping'], results=a['results'] + b['results'])
        self.assertFalse(self.check(sql, report)['alerts'])
        report['mapping'] = a['mapping']
        alert, = self.check(sql, report)['alerts']
        self.assertEqual((alert['reference'], alert['occurrence']), ('b', 2))

    def test_ambiguous_and_alias_hidden_names_are_not_assigned(self):
        for sql in ["SELECT * FROM s.events a JOIN s.target b ON a.id=b.id WHERE data_dt=1",
                    "SELECT * FROM s.events a WHERE events.data_dt=1",
                    "SELECT * FROM s.events a WHERE s.events.data_dt=1",
                    "SELECT * FROM events WHERE data_dt=1"]:
            with self.subTest(sql=sql):
                self.assertEqual(self.check(sql)['metrics']['expected_accesses'], 0)

    def test_multiple_constraints_dynamic_or_range_and_outer_joins_skip(self):
        for sql in ["SELECT * FROM s.events WHERE data_dt='01' AND data_dt='1'",
                    "SELECT * FROM s.events WHERE data_dt=1 AND data_dt>0",
                    "SELECT * FROM s.events WHERE data_dt=1 OR id=2",
                    "SELECT * FROM s.events WHERE NOT(data_dt=1)",
                    "SELECT * FROM s.events WHERE data_dt BETWEEN 1 AND 2",
                    "SELECT * FROM s.events WHERE data_dt=CURRENT_DATE",
                    "SELECT * FROM s.events a LEFT JOIN s.target b ON a.id=b.id WHERE a.data_dt=1",
                    "SELECT * FROM s.events a JOIN s.target b ON a.data_dt=2 WHERE a.data_dt=1"]:
            with self.subTest(sql=sql):
                checked = self.check(sql)
                self.assertFalse(checked['alerts'])
                self.assertTrue(checked['metrics']['skip_reason_counts'])

    def test_insert_values_column_position_and_update_old_new_values(self):
        sql = "INSERT INTO s.events(id,data_dt) VALUES (1,'a'),(2,'b')"
        self.assertFalse(self.check(sql, output([('string', 'a'), ('string', 'b')], role='INSERT写入'))['alerts'])
        alert, = self.check(sql, output([('string', 'a')], role='INSERT写入'))['alerts']
        self.assertEqual(alert['partition_value']['value'], 'b')
        update = "UPDATE s.events SET data_dt=2 WHERE data_dt=1"
        alerts = self.check(update, output([('number', '1')], role='UPDATE更新'))['alerts']
        self.assertEqual([item['partition_value']['value'] for item in alerts], ['2'])
        self.assertEqual(alerts[0]['origin'], 'UPDATE SET')
        delete = 'DELETE FROM s.events WHERE data_dt=1'
        self.assertFalse(self.check(delete, output([('number', '1')], role='DELETE删除'))['alerts'])

    def test_copy_failed_logs_and_nonconfigured_tables_are_excluded(self):
        for sql in ["COPY s.events(data_dt) FROM STDIN WITH CSV",
                    "An exception was encountered during the execution of statement: SELECT * FROM s.events WHERE data_dt=1",
                    "SELECT * FROM s.unknown WHERE data_dt=1",
                    "INSERT INTO s.events VALUES (1)"]:
            with self.subTest(sql=sql):
                self.assertFalse(self.check(sql)['alerts'])

    def test_cte_consumer_not_pushed_and_local_reference_ordinal_is_preserved(self):
        outer = 'WITH c AS (SELECT * FROM s.events) SELECT * FROM c WHERE data_dt=1'
        self.assertEqual(self.check(outer)['metrics']['expected_accesses'], 0)
        local = 'WITH c AS (SELECT * FROM s.events WHERE data_dt=1) SELECT * FROM c'
        self.assertFalse(self.check(local, output([('number', '1')]))['alerts'])
        pushed = 'WITH c AS (SELECT * FROM s.events WHERE data_dt=1) SELECT * FROM c WHERE data_dt=2'
        self.assertEqual(self.check(pushed)['metrics']['expected_accesses'], 0)
        scoped = ('SELECT * FROM (SELECT * FROM s.events a WHERE a.data_dt=1) x '
                  'JOIN (SELECT * FROM s.events a WHERE a.data_dt=2) y ON x.id=y.id')
        self.assertEqual(self.check(scoped)['metrics']['expected_accesses'], 0)

    def test_statement_ordinals_include_unconfigured_references(self):
        sql = 'SELECT * FROM s.plain; SELECT * FROM s.events WHERE data_dt=1'
        self.assertFalse(self.check(sql, output([('number', '1')], occurrence=2))['alerts'])
        alert, = self.check(sql, output([('number', '1')]))['alerts']
        self.assertEqual((alert['statement'], alert['occurrence']), (2, 2))

    def test_values_alias_and_greenplum_distribution_are_independent_compatibility(self):
        sql = 'SELECT * FROM s.events values WHERE values.data_dt=1'
        self.assertFalse(self.check(sql, output([('number', '1')]))['alerts'])
        ctas = "CREATE TABLE s.unconfigured AS SELECT * FROM s.events WHERE data_dt=1 DISTRIBUTED REPLICATED"
        self.assertFalse(self.check(ctas, output([('number', '1')], occurrence=2))['alerts'])
        legitimate = 'CREATE TABLE s.target AS SELECT * FROM s.distributed randomly'
        self.assertEqual(self.check(legitimate)['metrics']['expected_accesses'], 0)

    def test_explicit_partition_objects_are_checked(self):
        sql = 'ALTER TABLE s.events TRUNCATE PARTITION p20261007'
        report = output([], role='ALTER清空分区')
        identity = ('partition_ddl', 'TRUNCATE PARTITION p20261007')
        report['mapping'][0]['partition_identity'] = identity
        report['results'][0]['partition_identity'] = identity
        self.assertFalse(self.check(sql, report)['alerts'])
        report['mapping'] = []
        alert, = self.check(sql, report)['alerts']
        self.assertEqual(alert['partition_value'], {'type': 'partition_name', 'value': 'p20261007'})
        for_sql = "ALTER TABLE s.events DROP PARTITION FOR (DATE '20261007')"
        alert, = self.check(for_sql)['alerts']
        self.assertEqual(alert['partition_value']['type'], 'partition_for')

    def test_original_sql_users_and_log_counts_are_retained_and_progress_called(self):
        sql = 'SELECT * FROM s.events WHERE data_dt=1'
        p = {'sqls': profile(sql)['sqls'] + profile(sql, sid='Q2', user='bob', count=8)['sqls']}
        calls = []
        result = check_partition_omissions(p, {'partition_config': CONFIG, 'mapping': [], 'results': []},
                                          progress=lambda done, total: calls.append((done, total)))
        self.assertEqual(calls, [(2, 2)])
        self.assertEqual([(a['sql_id'], a['user_name'], a['log_count'], a['sql']) for a in result['alerts']],
                         [('Q1', 'alice', 3, sql), ('Q2', 'bob', 8, sql)])
        self.assertEqual(result['metrics']['supported_source_logs'], 11)

    def test_independence_when_main_analyzer_and_aggregator_are_unusable(self):
        sql = 'SELECT * FROM s.events WHERE data_dt=1'
        with patch('sql_partition_analysis.partitioning.analyzer.analyze_profile', side_effect=AssertionError), \
                patch('sql_partition_analysis.analytics.aggregation.aggregate_accesses', side_effect=AssertionError):
            alert, = self.check(sql)['alerts']
        self.assertEqual(alert['partition_value'], {'type': 'number', 'value': '1'})

    def test_timestamp_numeric_and_common_typed_date_in(self):
        sql = "SELECT * FROM s.events WHERE data_dt=TIMESTAMP '2026-10-07 12:00:00'"
        self.assertFalse(self.check(sql, output([('timestamp', '2026-10-07 12:00:00')]))['alerts'])
        number = 'SELECT * FROM s.events WHERE data_dt=-1.500'
        self.assertFalse(self.check(number, output([('number', '-1.5')]))['alerts'])
        dated = "SELECT * FROM s.events WHERE data_dt IN (DATE '20261007','20261008')"
        self.assertFalse(self.check(dated, output([('date', '2026-10-07'), ('date', '2026-10-08')]))['alerts'])

    def test_current_pipeline_outputs_agree_for_supported_evidence(self):
        from sql_partition_analysis.partitioning.analyzer import analyze_profile
        from sql_partition_analysis.partitioning.catalog import PartitionCatalog
        from sql_partition_analysis.analytics.aggregation import aggregate_accesses
        sqls = [
            "SELECT * FROM s.events WHERE data_dt=DATE '20261007'",
            "SELECT * FROM s.events WHERE data_dt='20261007'",
            "SELECT * FROM s.events WHERE data_dt IN ('a','b')",
            "SELECT * FROM s.events WHERE data_dt IN (DATE '20261007','20261008')",
            "SELECT * FROM s.events WHERE data_dt=TIMESTAMP '2026-10-07 12:00:00.123'",
            'SELECT * FROM s.events a JOIN s.events b ON a.id=b.id WHERE a.data_dt=1 AND b.data_dt=2',
            "INSERT INTO s.events(id,data_dt) VALUES (1,'a'),(2,'b')",
            'UPDATE s.events SET data_dt=2 WHERE data_dt=1',
            'DELETE FROM s.events WHERE data_dt=1',
            'WITH c AS (SELECT * FROM s.events WHERE data_dt=1) SELECT * FROM c',
            'CREATE TABLE s.target AS SELECT * FROM s.events WHERE data_dt=1 DISTRIBUTED REPLICATED',
            'SELECT * FROM s.events values WHERE values.data_dt=1',
            'ALTER TABLE s.events TRUNCATE PARTITION p20261007',
            "ALTER TABLE s.events DROP PARTITION FOR (DATE '20261007')",
        ]
        catalog = PartitionCatalog(CONFIG)
        for sql in sqls:
            with self.subTest(sql=sql):
                p = profile(sql)
                analysis = analyze_profile(p, catalog)
                report = aggregate_accesses(p, analysis, catalog)
                checked = check_partition_omissions(p, report)
                self.assertFalse(checked['alerts'], checked['alerts'])
                self.assertGreater(checked['metrics']['expected_accesses'], 0)

    def test_precision_and_try_cast_are_not_evaluated(self):
        for sql in ["SELECT * FROM s.events WHERE data_dt=CAST('2026-10-07 12:00:00.8' AS TIMESTAMP(0))",
                    "SELECT * FROM s.events WHERE data_dt=TRY_CAST('20261007' AS DATE)"]:
            with self.subTest(sql=sql):
                checked = self.check(sql)
                self.assertFalse(checked['alerts'])
                self.assertEqual(checked['metrics']['expected_accesses'], 0)

    def test_unsupported_statement_does_not_shift_following_reference(self):
        for prefix in ["COMMENT ON TABLE s.events IS 'note'; ",
                       'ALTER TABLE IF EXISTS s.events ADD COLUMN flag integer; ']:
            with self.subTest(prefix=prefix):
                sql = prefix + 'SELECT * FROM s.events WHERE data_dt=1'
                checked = self.check(sql, output([('number', '1')], occurrence=2))
                self.assertFalse(checked['alerts'])
                self.assertEqual(checked['metrics']['expected_accesses'], 0)

    def test_join_only_conditions_are_explicit_coverage_gap(self):
        checked = self.check('SELECT * FROM s.events a JOIN s.target b ON a.data_dt=1')
        self.assertFalse(checked['alerts'])
        self.assertIn('non_where_conditions_not_checked', checked['metrics']['skip_reason_counts'])

    def test_large_negative_numeric_literal_is_not_rounded(self):
        exact = '-123456789012345678901234567890.123'
        sql = 'SELECT * FROM s.events WHERE data_dt=' + exact
        self.assertFalse(self.check(sql, output([('number', exact)]))['alerts'])
        rounded = '-123456789012345678901234567900'
        alert, = self.check(sql, output([('number', rounded)]))['alerts']
        self.assertEqual(alert['partition_value'], {'type': 'number', 'value': exact})

    def test_scalar_casts_have_independent_exact_type_and_scale_oracles(self):
        examples = [
            ("CAST('001' AS INTEGER)", 'number', '1'),
            ("CAST('1.5' AS NUMERIC)", 'number', '1.5'),
            ('CAST(1.50 AS TEXT)', 'string', '1.50'),
            ("CAST(CAST('001' AS INTEGER) AS TEXT)", 'string', '1'),
            ("CAST(CAST('001.500' AS NUMERIC) AS VARCHAR)", 'string', '1.500'),
            ('CAST(1.000 AS INTEGER)', 'number', '1'),
            ("CAST('-32768' AS SMALLINT)", 'number', '-32768'),
            ("CAST('9223372036854775807' AS BIGINT)", 'number', '9223372036854775807'),
            ('CAST(-0.00 AS TEXT)', 'string', '0.00'),
        ]
        for expression, kind, value in examples:
            with self.subTest(expression=expression):
                sql = 'SELECT * FROM s.events WHERE data_dt=' + expression
                self.assertFalse(self.check(sql, output([(kind, value)]))['alerts'])
                legacy_unknown = output([])
                for row in legacy_unknown['mapping'] + legacy_unknown['results']:
                    row['partition_identity'] = ('mixed', (), ('unexpanded CAST',))
                alert, = self.check(sql, legacy_unknown)['alerts']
                self.assertEqual(alert['partition_value'], {'type': kind, 'value': value})
        scaled = 'SELECT * FROM s.events WHERE data_dt=CAST(1.50 AS TEXT)'
        self.assertTrue(self.check(scaled, output([('string', '1.5')]))['alerts'])

    def test_unproved_cast_rounding_precision_bounds_and_session_semantics_skip(self):
        expressions = [
            'CAST(1.5 AS INTEGER)', "CAST('1.0' AS INTEGER)",
            "CAST('32768' AS SMALLINT)", "CAST('-2147483649' AS INTEGER)",
            "CAST('9223372036854775808' AS BIGINT)",
            "CAST('1.5' AS NUMERIC(10,2))", 'CAST(1.50 AS VARCHAR(1))',
            'CAST(CAST(1.50 AS DOUBLE PRECISION) AS TEXT)',
            "CAST(DATE '20261007' AS TEXT)",
            "CAST(TIMESTAMP '2026-10-07 12:00:00' AS TEXT)",
            "CAST('NaN' AS NUMERIC)", "TRY_CAST('001' AS INTEGER)",
        ]
        for expression in expressions:
            with self.subTest(expression=expression):
                checked = self.check('SELECT * FROM s.events WHERE data_dt=' + expression)
                self.assertFalse(checked['alerts'])
                self.assertEqual(checked['metrics']['expected_accesses'], 0)

    def test_unary_negation_preserves_explicit_integer_width(self):
        valid = [
            ("-CAST('-32767' AS SMALLINT)", '32767'),
            ("-CAST('32767' AS SMALLINT)", '-32767'),
            ("-CAST('-2147483647' AS INTEGER)", '2147483647'),
            ("-CAST('-9223372036854775807' AS BIGINT)", '9223372036854775807'),
            ("-CAST(CAST('-32768' AS SMALLINT) AS INTEGER)", '32768'),
            ("-CAST(CAST('-2147483648' AS INTEGER) AS BIGINT)", '2147483648'),
            ("-CAST(CAST('-9223372036854775808' AS BIGINT) AS NUMERIC)", '9223372036854775808'),
        ]
        for expression, value in valid:
            with self.subTest(expression=expression):
                sql = 'SELECT * FROM s.events WHERE data_dt=' + expression
                self.assertFalse(self.check(sql, output([('number', value)]))['alerts'])
                alert, = self.check(sql)['alerts']
                self.assertEqual(alert['partition_value']['value'], value)
        invalid = [
            "-CAST('-32768' AS SMALLINT)",
            "-CAST('-2147483648' AS INTEGER)",
            "-CAST('-9223372036854775808' AS BIGINT)",
            "-CAST(CAST('-32768' AS INTEGER) AS SMALLINT)",
            "CAST(-CAST('-2147483648' AS INTEGER) AS BIGINT)",
        ]
        for expression in invalid:
            with self.subTest(expression=expression):
                checked = self.check('SELECT * FROM s.events WHERE data_dt=' + expression)
                self.assertFalse(checked['alerts'])
                self.assertEqual(checked['metrics']['expected_accesses'], 0)

    def test_numeric_storage_bounds_skip_before_rendering_large_values(self):
        import sqlglot
        from sql_partition_analysis.validation.partition_omissions import _scalar
        invalid = ["CAST('1e-16384' AS NUMERIC)", "CAST('0e-16384' AS NUMERIC)",
                   "CAST('1e131072' AS NUMERIC)", '1e-16384', '0e-16384', '1e131072',
                   "CAST(CAST('1e-16384' AS NUMERIC) AS TEXT)"]
        for expression in invalid:
            with self.subTest(expression=expression):
                checked = self.check('SELECT * FROM s.events WHERE data_dt=' + expression)
                self.assertFalse(checked['alerts'])
                self.assertEqual(checked['metrics']['expected_accesses'], 0)
        # Inspect short Decimal exponents directly, keeping boundary tests from
        # constructing or printing 131,072-digit partition presentation labels.
        for expression, exponent in [("CAST('1e131071' AS NUMERIC)", 131071),
                                     ("CAST('1e-16383' AS NUMERIC)", -16383),
                                     ("CAST('0e-16383' AS NUMERIC)", -16383)]:
            with self.subTest(expression=expression):
                scalar = _scalar(sqlglot.parse_one(expression, read='postgres'))
                self.assertEqual(scalar[0], 'number')
                self.assertEqual(scalar[1].as_tuple().exponent, exponent)
        text = "SELECT * FROM s.events WHERE data_dt=CAST('1e-16384' AS TEXT)"
        self.assertFalse(self.check(text, output([('string', '1e-16384')]))['alerts'])

    def test_tuple_update_single_constant_row_detects_missing_new_value(self):
        for source in ['(SELECT 2,9)', '(VALUES (2,9))', '(2,9)',
                       "(SELECT CAST('002' AS INTEGER) AS dt,9 AS id)"]:
            with self.subTest(source=source):
                sql = 'UPDATE s.events SET (data_dt,id)=' + source + ' WHERE data_dt=1'
                alert, = self.check(sql, output([('number', '1')], role='UPDATE更新'))['alerts']
                self.assertEqual(alert['partition_value'], {'type': 'number', 'value': '2'})
                self.assertEqual(alert['origin'], 'UPDATE SET')
                self.assertFalse(self.check(sql, output([('number', '1'), ('number', '2')], role='UPDATE更新'))['alerts'])

    def test_tuple_update_source_cardinality_or_nonconstant_projection_is_not_guessed(self):
        sources = [
            '(SELECT 2,9 FROM s.plain)', '(SELECT 2,9 WHERE FALSE)',
            '(SELECT 2,COUNT(*))', '(SELECT 2,ROW_NUMBER() OVER ())',
            '(SELECT 2,generate_series(1,2))', '(SELECT 2,9 LIMIT 0)',
            '(SELECT 2,9 OFFSET 1)', '(SELECT 2,9 GROUP BY 1)',
            '(SELECT 2,9 HAVING FALSE)', '(SELECT 2,9 UNION ALL SELECT 3,8)',
            '(WITH x AS (SELECT 1) SELECT 2,9)', '(VALUES (2,9),(3,8))',
            '(SELECT data_dt,9)', '(SELECT 2)', '(SELECT 2,9,8)',
            '((SELECT 2,9) LIMIT 0)', '((SELECT 2,9) OFFSET 1)',
            '((SELECT 2,9) FETCH FIRST 0 ROWS ONLY)',
        ]
        for source in sources:
            with self.subTest(source=source):
                sql = 'UPDATE s.events SET (data_dt,id)=' + source + ' WHERE data_dt=1'
                checked = self.check(sql, output([('number', '1')], role='UPDATE更新'))
                self.assertFalse(checked['alerts'])
                self.assertEqual(checked['metrics']['expected_accesses'], 1)
                self.assertIn('unproved_update_tuple_source', checked['metrics']['skip_reason_counts'])

    def test_main_fixed_cast_and_single_row_update_results_satisfy_independent_checker(self):
        from sql_partition_analysis.partitioning.analyzer import analyze_profile
        from sql_partition_analysis.partitioning.catalog import PartitionCatalog
        from sql_partition_analysis.analytics.aggregation import aggregate_accesses
        examples = [
            "SELECT * FROM s.events WHERE data_dt=CAST('001' AS INTEGER)",
            "SELECT * FROM s.events WHERE data_dt=CAST('1.5' AS NUMERIC)",
            'SELECT * FROM s.events WHERE data_dt=CAST(1.50 AS TEXT)',
            'SELECT * FROM s.events WHERE data_dt=CAST(1.000 AS INTEGER)',
            'SELECT * FROM s.events WHERE data_dt=CAST(-0.00 AS TEXT)',
            'UPDATE s.events SET (data_dt,id)=(SELECT 2,9) WHERE data_dt=1',
            'UPDATE s.events SET (data_dt,id)=(VALUES (2,9)) WHERE data_dt=1',
        ]
        catalog = PartitionCatalog(CONFIG)
        for sql in examples:
            with self.subTest(sql=sql):
                p = profile(sql)
                report = aggregate_accesses(p, analyze_profile(p, catalog), catalog)
                checked = check_partition_omissions(p, report)
                self.assertFalse(checked['alerts'])
                if sql.startswith('UPDATE'):
                    report['mapping'] = [row for row in report['mapping']
                                         if ('data_dt', 'number', '2') not in row['partition_identity'][1]]
                else:
                    report['mapping'] = []
                self.assertTrue(check_partition_omissions(p, report)['alerts'])


if __name__ == '__main__':
    unittest.main()
