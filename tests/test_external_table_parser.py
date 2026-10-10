"""Greenplum CUSTOM format evidence at the pinned SQLGlot boundary."""
import _support
import unittest

import sqlglot
from sqlglot import exp
from sqlglot.dialects.postgres import Postgres
from sqlglot.errors import ParseError

from sql_partition_analysis.analytics.aggregation import aggregate_accesses
from sql_partition_analysis.partitioning.analyzer import analyze_profile
from sql_partition_analysis.partitioning.catalog import PartitionCatalog
from sql_partition_analysis.partitioning.parser import (
    compatible_postgres, parse_one_postgres, parse_postgres,
)


class ExternalTableParserTests(unittest.TestCase):
    @staticmethod
    def statement(options="formatter='pxfwritable_import'", prefix='EXTERNAL',
                  location="LOCATION ('pxf://warehouse/a?PROFILE=hdfs:parquet&SERVER=10')"):
        return ("CREATE {} TABLE exdata.events (data_dt DATE, id INT) {} "
                "FORMAT 'CUSTOM' ({})").format(prefix, location, options)

    @staticmethod
    def options(tree):
        formats = list(tree.find_all(exp.FileFormatProperty))
        assert len(formats) == 1
        custom = formats[0].this
        assert isinstance(custom, exp.Anonymous) and custom.name == 'CUSTOM'
        return {option.name: option.args['value'] for option in custom.expressions}

    @staticmethod
    def profile(sql):
        return {'sqls': [{'id': 'SQL000001', 'sql': sql, 'count': 3,
                         'first_time': '2026-10-07 00:00:00',
                         'last_time': '2026-10-07 00:00:10'}], 'widths': {'test': 3}}

    def test_formatter_and_every_option_survive_as_native_ast_nodes(self):
        sql = self.statement("FORMATTER=etl.parquet_import, delimiter=';', "
                             "batch=1000, offset=-2.5, threads=+3, columns=(id,data_dt)",
                             location="LOCATION ('pxf://a','gpfdist://b')")
        tree = parse_one_postgres(sql)
        self.assertIsInstance(tree, exp.Create)
        self.assertEqual((tree.this.this.db, tree.this.this.name), ('exdata', 'events'))
        self.assertEqual([column.name for column in tree.this.expressions], ['data_dt', 'id'])
        options = self.options(tree)
        self.assertEqual(options['FORMATTER'].sql(dialect='postgres'), 'etl.parquet_import')
        self.assertEqual(options['delimiter'], exp.Literal.string(';'))
        self.assertEqual(options['batch'], exp.Literal.number('1000'))
        self.assertEqual(options['offset'], exp.Neg(this=exp.Literal.number('2.5')))
        self.assertEqual(options['threads'], exp.Literal.number('3'))
        self.assertEqual([column.name for column in options['columns'].expressions], ['id', 'data_dt'])
        locations = list(tree.find_all(exp.LocationProperty))[0].this
        self.assertEqual([literal.this for literal in locations.find_all(exp.Literal)],
                         ['pxf://a', 'gpfdist://b'])
        rendered = tree.sql(dialect='postgres', unsupported_level='RAISE')
        self.assertIn('FORMAT=CUSTOM(FORMATTER=etl.parquet_import', rendered)
        self.assertIn('columns=(id, data_dt)', rendered)
        self.assertEqual(compatible_postgres(sql), sql)

    def test_quoted_identifiers_comments_case_and_string_content_are_preserved(self):
        sql = ('CrEaTe /* DDL */ ExTeRnAl TaBlE "Case Schema"."Events" '
               '("Date" DATE, "ID" INT) LOCATION (\'pxf://a?x=(FORMATTER=)\') '
               'FoRmAt \'CuStOm\' (/* option */ "Formatter"="ETL"."FormatFn", '
               '"Parameter"=\'x, FORMATTER=y ) \'\' z\')')
        tree = parse_one_postgres(sql)
        self.assertEqual((tree.this.this.db, tree.this.this.name), ('Case Schema', 'Events'))
        formatter = self.options(tree)['Formatter']
        self.assertEqual(formatter.sql(dialect='postgres'), '"ETL"."FormatFn"')
        self.assertEqual(self.options(tree)['Parameter'].this, "x, FORMATTER=y ) ' z")
        self.assertIn('pxf://a?x=(FORMATTER=)', tree.sql(dialect='postgres'))
        self.assertEqual(compatible_postgres(sql), sql)

    def test_external_modes_web_and_temp_stay_in_the_renderable_ast(self):
        for prefix in ('EXTERNAL', 'READABLE EXTERNAL', 'WRITABLE EXTERNAL',
                       'EXTERNAL WEB', 'READABLE EXTERNAL WEB',
                       'WRITABLE EXTERNAL WEB TEMP'):
            with self.subTest(prefix=prefix):
                tree = parse_one_postgres(self.statement(prefix=prefix))
                self.assertIsInstance(tree, exp.Create)
                properties = tree.args['properties'].expressions
                self.assertEqual(len([prop for prop in properties if isinstance(prop, exp.ExternalProperty)]), 1)
                flags = {prop.name: prop.args['value'] for prop in properties
                         if type(prop) is exp.Property}
                mode = next((mode for mode in ('READABLE', 'WRITABLE') if mode in prefix), None)
                self.assertEqual(flags.get('EXTERNAL_MODE'), exp.Literal.string(mode) if mode else None)
                self.assertEqual(flags.get('EXTERNAL_WEB'), exp.Boolean(this=True) if 'WEB' in prefix else None)
                rendered = tree.sql(dialect='postgres', unsupported_level='RAISE')
                if mode:
                    self.assertIn("EXTERNAL_MODE='{}'".format(mode), rendered)
                if 'WEB' in prefix:
                    self.assertIn('EXTERNAL_WEB=TRUE', rendered)
                if 'TEMP' in prefix:
                    self.assertIn('TEMPORARY', rendered)

    def test_new_external_modes_only_expand_validated_custom_formats(self):
        prefixes = ('READABLE EXTERNAL', 'WRITABLE EXTERNAL', 'EXTERNAL WEB',
                    'READABLE EXTERNAL WEB', 'WRITABLE EXTERNAL WEB TEMP')
        formats = ('', " FORMAT 'TEXT'", " FORMAT 'CSV'", ' FORMAT CUSTOM(1)',
                   ' FORMAT "CUSTOM"')
        query = " AS SELECT * FROM s.events WHERE data_dt=DATE '2026-10-07'"
        catalog = PartitionCatalog({'version': 1, 'tables': {
            's.events': {'partition_columns': ['data_dt']}}})
        for prefix in prefixes:
            for format_clause in formats:
                for suffix in ('', query):
                    with self.subTest(prefix=prefix, format=format_clause, query=bool(suffix)):
                        sql = ("CREATE {} TABLE exdata.events (id INT) LOCATION ('pxf://a')"
                               "{}{}").format(prefix, format_clause, suffix)
                        tree = parse_one_postgres(sql)
                        self.assertIsInstance(tree, exp.Command)
                        self.assertEqual(tree, sqlglot.parse_one(sql, read=Postgres))
                        analysis = analyze_profile(self.profile(sql), catalog)
                        self.assertEqual(analysis['index'][0]['status'], '语法未展开')
                        self.assertEqual(analysis['details'], [])
                        self.assertEqual(analysis['predicates'], [])
                        self.assertEqual(aggregate_accesses(self.profile(sql), analysis, catalog)['mapping'], [])
            with self.subTest(prefix=prefix, format='CUSTOM', query=True):
                with self.assertRaises(ParseError):
                    parse_one_postgres(self.statement(prefix=prefix) + query)

    def test_opaque_external_mode_statement_preserves_following_query(self):
        ddl = ("CREATE READABLE EXTERNAL TABLE exdata.events (id INT) LOCATION ('pxf://a') "
               "FORMAT 'TEXT' AS SELECT * FROM s.hidden WHERE data_dt=DATE '2026-10-06'")
        sql = ddl + "; SELECT * FROM s.events WHERE data_dt=DATE '2026-10-07'"
        trees = parse_postgres(sql)
        self.assertEqual([type(tree) for tree in trees], [exp.Command, exp.Select])
        self.assertEqual(trees[0], sqlglot.parse_one(ddl, read=Postgres))
        analysis = analyze_profile(self.profile(sql), PartitionCatalog({'version': 1, 'tables': {
            's.events': {'partition_columns': ['data_dt']},
            's.hidden': {'partition_columns': ['data_dt']}}}))
        self.assertEqual({row['table'] for row in analysis['details']}, {'s.events'})
        self.assertEqual({row['table'] for row in analysis['predicates']}, {'s.events'})

    def test_custom_web_execute_and_multiple_statements(self):
        sql = self.statement(prefix='READABLE EXTERNAL WEB', location="EXECUTE 'load-data.sh'")
        sql += "; SELECT * FROM s.events WHERE data_dt=DATE '2026-10-07'"
        trees = parse_postgres(sql)
        self.assertEqual([type(tree) for tree in trees], [exp.Create, exp.Select])
        command = trees[0].find(exp.ExecuteAsProperty)
        self.assertEqual(command.this, exp.Literal.string('load-data.sh'))
        profile = self.profile(sql)
        analysis = analyze_profile(profile, PartitionCatalog({'version': 1, 'tables': {
            's.events': {'partition_columns': ['data_dt']}}}))
        self.assertEqual(analysis['index'][0]['status'], '已解析')
        self.assertEqual({(row['table'], row['role']) for row in analysis['details']},
                         {('exdata.events', '建表'), ('s.events', '读取')})
        self.assertEqual({(row['table'], row['field']) for row in analysis['predicates']},
                         {('s.events', 'data_dt')})

    def test_missing_or_malformed_options_never_become_parsed_ddl(self):
        invalid_options = ["", "delimiter=';'", "formatter=", "formatter=''",
                           "formatter=1", "formatter=()", "formatter=(id)",
                           "formatter='a', formatter='b'", "formatter='a',",
                           "formatter='a' delimiter=';'", "formatter='a', n=1+2",
                           "formatter='a', n=(SELECT 1)", "formatter=f()",
                           "formatter='a', n=CAST(1 AS INT)", "formatter='a', n=-id",
                           "formatter=etl.", "formatter='a', n=(id,)",
                           "'formatter'='a'", "formatter='a', n=$1"]
        for options in invalid_options:
            with self.subTest(options=options):
                with self.assertRaises(ParseError):
                    parse_one_postgres(self.statement(options))
        sql = self.statement()
        for truncated in (sql[:-1], sql.rsplit('(', 1)[0], sql.rsplit('FORMAT', 1)[0] + "FORMAT 'CUSTOM'"):
            with self.subTest(truncated=truncated):
                with self.assertRaises(ParseError):
                    parse_one_postgres(truncated)
        with self.assertRaises(ParseError):
            parse_one_postgres(self.statement(location=''))
        with self.assertRaises(ParseError):
            parse_one_postgres(self.statement(location="EXECUTE 'load-data.sh'"))

    def test_custom_locations_and_duplicate_sources_are_validated_before_normalizing(self):
        sources = ["LOCATION ()", "LOCATION (SELECT 1)", "LOCATION (1)",
                   "LOCATION ('x',)", "LOCATION ('x',,'y')", "LOCATION (x)",
                   "LOCATION ('x'+ 'y')", "LOCATION 'x'", "LOCATION ($1)",
                   "LOCATION ('x') LOCATION ('y')",
                   "LOCATION ('x') EXECUTE 'cmd'"]
        for source in sources:
            with self.subTest(source=source):
                with self.assertRaises(ParseError):
                    parse_one_postgres(self.statement(location=source))
        sql = self.statement()
        for suffix in (" FORMAT 'CUSTOM' (formatter='g')", " FORMAT 'TEXT'",
                       " LOCATION ('y')", " EXECUTE 'cmd'"):
            with self.subTest(suffix=suffix):
                with self.assertRaises(ParseError):
                    parse_one_postgres(sql + suffix)
        valid = self.statement(location="LOCATION ('x','y')")
        locations = parse_one_postgres(valid).find(exp.LocationProperty).this
        self.assertEqual([value.this for value in locations.find_all(exp.Literal)], ['x', 'y'])

    def test_custom_web_execute_rejects_expressions_and_duplicate_commands(self):
        for source in ("EXECUTE 1", "EXECUTE (SELECT 1)", "EXECUTE ('cmd')",
                       "EXECUTE cmd", "EXECUTE $1", "EXECUTE 'cmd' EXECUTE 'other'",
                       "EXECUTE 'cmd' LOCATION ('x')"):
            with self.subTest(source=source):
                with self.assertRaises(ParseError):
                    parse_one_postgres(self.statement(prefix='EXTERNAL WEB', location=source))
        # Empty command text is retained as evidence; executing or validating
        # the command's runtime meaning is outside SQL access analysis.
        tree = parse_one_postgres(self.statement(prefix='EXTERNAL WEB', location="EXECUTE ''"))
        self.assertEqual(tree.find(exp.ExecuteAsProperty).this, exp.Literal.string(''))

    def test_custom_field_group_and_clause_order_do_not_create_false_query_accesses(self):
        sqls = ["CREATE EXTERNAL TABLE t LOCATION ('x') FORMAT 'CUSTOM' (formatter='f')",
                "CREATE EXTERNAL TABLE t (id INT,) LOCATION ('x') FORMAT 'CUSTOM' (formatter='f')",
                "CREATE EXTERNAL TABLE t (,id INT) LOCATION ('x') FORMAT 'CUSTOM' (formatter='f')",
                "CREATE EXTERNAL TABLE t (id INT,,dt DATE) LOCATION ('x') FORMAT 'CUSTOM' (formatter='f')",
                "CREATE EXTERNAL TABLE t (id) LOCATION ('x') FORMAT 'CUSTOM' (formatter='f')",
                "CREATE EXTERNAL TABLE t (id INT,dt) LOCATION ('x') FORMAT 'CUSTOM' (formatter='f')",
                self.statement() + " AS SELECT * FROM s.events WHERE data_dt=DATE '2026-10-07'",
                "CREATE EXTERNAL TABLE t (id INT) FORMAT 'CUSTOM' (formatter='f') LOCATION ('x')",
                "CREATE EXTERNAL WEB TABLE t (id INT) FORMAT 'CUSTOM' (formatter='f') EXECUTE 'cmd'"]
        for sql in sqls:
            with self.subTest(sql=sql):
                with self.assertRaises(ParseError):
                    parse_one_postgres(sql)
        analysis = analyze_profile(self.profile(sqls[6]), PartitionCatalog({'version': 1, 'tables': {
            's.events': {'partition_columns': ['data_dt']}}}))
        self.assertEqual(analysis['index'][0]['status'], '解析失败')
        self.assertEqual(analysis['details'], [])
        self.assertEqual(analysis['predicates'], [])
        # Greenplum's OptExtTableElementList allows an explicitly empty group.
        tree = parse_one_postgres("CREATE EXTERNAL TABLE t () LOCATION ('x') "
                                  "FORMAT 'CUSTOM' (formatter='f')")
        self.assertIsInstance(tree.this, exp.Schema)
        self.assertEqual(tree.this.expressions, [])

    def test_custom_field_separator_checks_respect_nested_delimiters(self):
        # This check validates field separators/types, not every Greenplum
        # column constraint. Nested commas retain the pinned native AST.
        sql = ("CREATE EXTERNAL TABLE t (amount NUMERIC(10,2), "
               "id INT DEFAULT COALESCE(1,2), ids INT[] DEFAULT ARRAY[1,2]) "
               "LOCATION ('x') FORMAT 'CUSTOM' (formatter='f')")
        tree = parse_one_postgres(sql)
        self.assertIsInstance(tree, exp.Create)
        self.assertEqual([column.name for column in tree.this.expressions], ['amount', 'id', 'ids'])
        self.assertTrue(all(isinstance(column.args['kind'], exp.DataType) for column in tree.this.expressions))
        self.assertIn('DECIMAL(10, 2)', tree.sql(dialect='postgres'))
        self.assertIn('COALESCE(1, 2)', tree.sql(dialect='postgres'))
        self.assertIn('ARRAY[1, 2]', tree.sql(dialect='postgres'))

    def test_escape_strings_keep_literal_types_and_other_literal_tokens_are_not_identifiers(self):
        sql = self.statement("formatter=E'etl.f', arg=E'x\\ny', empty=E''",
                             location="LOCATION (E'pxf://x')").replace("FORMAT 'CUSTOM'", "FORMAT E'CUSTOM'")
        tree = parse_one_postgres(sql)
        options = self.options(tree)
        self.assertEqual(options['formatter'], exp.ByteString(this='etl.f'))
        self.assertEqual(options['arg'], exp.ByteString(this='x\\ny'))
        self.assertEqual(options['empty'], exp.ByteString(this=''))
        self.assertIsInstance(tree.find(exp.LocationProperty).this.this, exp.ByteString)
        rendered = tree.sql(dialect='postgres', unsupported_level='RAISE')
        self.assertIn("formatter=e'etl.f'", rendered)
        self.assertIn("arg=e'x\\ny'", rendered)
        web = parse_one_postgres(self.statement(prefix='EXTERNAL WEB', location="EXECUTE E'cmd'"))
        self.assertIsInstance(web.find(exp.ExecuteAsProperty).this, exp.ByteString)
        for options in ("formatter=E''", "formatter='f', arg=X'AF'",
                        "formatter='f', arg=B'10'", "formatter=X'AF'",
                        "E'formatter'='f'", "formatter='f', X'AF'=1"):
            with self.subTest(options=options):
                with self.assertRaises(ParseError):
                    parse_one_postgres(self.statement(options))

    def test_illegal_external_prefixes_do_not_activate_compatibility(self):
        for prefix in ('READABLE WRITABLE EXTERNAL', 'EXTERNAL READABLE',
                       'EXTERNAL WEB WEB', 'READABLE', 'WEB EXTERNAL'):
            with self.subTest(prefix=prefix):
                try:
                    tree = parse_one_postgres(self.statement(prefix=prefix))
                except ParseError:
                    continue
                self.assertIsInstance(tree, exp.Command)
                self.assertNotIn('FORMAT=CUSTOM(', tree.sql(dialect='postgres'))

    def test_ordinary_postgres_format_and_queries_keep_the_pinned_ast(self):
        sqls = ["CREATE TABLE s.events (id INT) FORMAT 'CUSTOM'",
                "CREATE TABLE s.events (id INT) FORMAT CUSTOM(1)",
                "CREATE TABLE s.events (id INT) FORMAT CUSTOM(1) LOCATION (1)",
                "CREATE TABLE s.events (id INT) LOCATION ()",
                "CREATE TABLE s.events (id INT) LOCATION (1)",
                "CREATE TABLE s.events (id INT) LOCATION ('x',)",
                "CREATE EXTERNAL TABLE s.events (id INT) LOCATION (1) FORMAT 'TEXT'",
                "CREATE EXTERNAL TABLE s.events (id INT) LOCATION ('x') FORMAT 'TEXT' "
                "AS SELECT * FROM s.source",
                "CREATE TABLE s.events (id INT) FORMAT 'TEXT'",
                "SELECT 'FORMAT ''CUSTOM'' (FORMATTER=''f'')' AS formatter",
                "SELECT format('%s', formatter) FROM s.events",
                "SELECT $$CREATE EXTERNAL TABLE x FORMAT 'CUSTOM' (FORMATTER='f')$$"]
        for sql in sqls:
            with self.subTest(sql=sql):
                self.assertEqual(parse_one_postgres(sql), sqlglot.parse_one(sql, read=Postgres))
                self.assertEqual(compatible_postgres(sql), sql)

    def test_creating_an_external_table_does_not_invent_partition_access(self):
        sql = self.statement()
        profile = self.profile(sql)
        catalog = PartitionCatalog({'version': 1, 'tables': {
            'exdata.events': {'partition_columns': ['data_dt']}}})
        analysis = analyze_profile(profile, catalog)
        self.assertEqual(analysis['index'][0]['status'], '已解析')
        self.assertEqual([(row['table'], row['role']) for row in analysis['details']], [('exdata.events', '建表')])
        self.assertEqual(analysis['predicates'], [])
        result = aggregate_accesses(profile, analysis, catalog)
        self.assertEqual(len(result['mapping']), 1)
        self.assertEqual(result['mapping'][0]['role'], 'CREATE建表')
        self.assertEqual(result['mapping'][0]['partition'], '表级操作（未指定分区）')
        self.assertNotEqual(result['mapping'][0]['partition_identity'][0], 'values')


if __name__ == '__main__':
    unittest.main()
