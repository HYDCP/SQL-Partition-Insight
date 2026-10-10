"""PostgreSQL positional parameters must not corrupt later quote boundaries."""
import _support
import unittest
from unittest.mock import patch

from sqlglot import exp
from sqlglot.dialects.postgres import Postgres
from sqlglot.errors import TokenError
from sqlglot.tokens import TokenType

from sql_partition_analysis.analytics.aggregation import aggregate_accesses
from sql_partition_analysis.partitioning.analyzer import analyze_profile
from sql_partition_analysis.partitioning.catalog import PartitionCatalog
from sql_partition_analysis.partitioning.lexical import LexicalPostgres, tokenize_postgres
from sql_partition_analysis.partitioning.operations import (
    parse_alter, recover_copy_header, split_sql_statements,
)
from sql_partition_analysis.partitioning.parser import (
    CompatiblePostgres, compatible_postgres, parse_one_postgres,
)
from sql_partition_analysis.partitioning.preprocessing import prepare_sql
from sql_partition_analysis.validation.partition_omissions import (
    _AuditPostgres, _split, check_partition_omissions,
)


CONFIG = {'version': 1, 'tables': {
    's.events': {'partition_columns': ['data_dt'], 'partition_type': 'RANGE'}}}

# Exact original SQL bodies from the full ZIP regression, including log line
# separators. The schema and parameter values are deliberately not invented.
ORIGINAL_PARAMETER_SQL = (
    ('SQL004023', "select oper_no as operNo,\x18\t\t\t   oper_name as operName,\x18\t\t\t   org_num as orgNum,\x18\t\t       org_name as orgName,\x18               org_lv_1 as orgLv1,\x18\t\t\t   org_lv_name_1 as orgLvName1,\x18\t\t\t   org_lv_2 as orgLv2,\x18\t\t\t   org_lv_name_2 as orgLvName2,\x18\t\t\t   org_lv_3 as orgLv3,\x18\t\t\t   org_lv_name_3 as orgLvName3,\x18\t\t\t   org_lv_4 as orgLv4,\x18\t\t\t   org_lv_name_4 as orgLvName4,\x18\t\t\t   org_lv_5 as orgLv5,\x18\t\t\t   org_lv_name_5 as orgLvName5,\x18\t\t\t   highest_org as highestOrg,\x18\t\t\t   highest_org_name as highestOrgName\x18\t\tfrom a_oper_org_rela where oper_no = $1 and highest_org is not null and highest_org != ''"),
    ('SQL004025', "select oper_no as operNo,\x18\t\t\t   oper_name as operName,\x18\t\t\t   org_num as orgNum,\x18\t\t       org_name as orgName,\x18               org_lv_1 as orgLv1,\x18\t\t\t   org_lv_name_1 as orgLvName1,\x18\t\t\t   case  when org_lv_2 = 'FE003000' then   org_lv_3 else org_lv_2 end as orgLv2,\x18\t\t\t   case  when org_lv_2 = 'FE003000' then  org_lv_name_3 else org_lv_name_2 end  as orgLvName2,\x18\t\t\t   org_lv_4 as orgLv4,\x18\t\t\t   org_lv_name_4 as orgLvName4,\x18\t\t\t   org_lv_5 as orgLv5,\x18\t\t\t   org_lv_name_5 as orgLvName5,\x18\t\t\t   highest_org as highestOrg,\x18\t\t\t   highest_org_name as highestOrgName\x18\t\tfrom a_oper_org_rela where oper_no = $1\x18\t\tand highest_org is not null\x18\t\tand highest_org != ''\x18\t\tand org_lv_2 is not null\x18\t\tand org_lv_2 != ''\x18\t\tand org_lv_3 is not null\x18\t\tand org_lv_3 != ''"),
    ('SQL061364', "select *\x18        from a_oper_org_rela_actg where oper_no = $1 and highest_org is not null and highest_org != ''"),
    ('SQL063953', "select\x18\t\t\t   org_lv_2 as orgLv2,\x18\t\t\t   org_lv_name_2 as orgLvName2,\x18\t\t\t   highest_org as highestOrg,\x18\t\t\t   highest_org_name as highestOrgName\x18\t\tfrom a_oper_org_rela where oper_no = $1 and highest_org is not null and highest_org != ''"),
)


def profile(sql, sid='Q1'):
    return {'sqls': [{'id': sid, 'sql': sql, 'count': 1, 'user_name': 'u',
                     'first_record': 1, 'types': ['SELECT'],
                     'first_time': '2026-10-09 00:00:00',
                     'last_time': '2026-10-09 00:00:00'}], 'widths': {'33': 1}}


class PostgresTokenizerTests(unittest.TestCase):
    def analyze(self, sql, config=CONFIG):
        catalog = PartitionCatalog(config)
        source = profile(sql)
        analysis = analyze_profile(source, catalog)
        return source, analysis, aggregate_accesses(source, analysis, catalog)

    def test_positional_parameter_before_terminal_empty_string(self):
        for sql in ("SELECT $1, ''", "SELECT $12, $2, ''", "SELECT $1, ''''",
                    "SELECT $1, 'a''b'", "SELECT $1, '' -- complete comment"):
            with self.subTest(sql=sql):
                tokens = tokenize_postgres(sql)
                self.assertTrue(any(t.token_type == TokenType.PARAMETER for t in tokens))
                self.assertEqual(compatible_postgres(sql), sql)
                node = parse_one_postgres(sql)
                self.assertTrue(list(node.find_all(exp.Parameter)))
                for token in tokens:
                    self.assertGreaterEqual(token.start, 0)
                    self.assertLess(token.end, len(sql))
        self.assertEqual(tokenize_postgres("SELECT $1, ''")[-1].text, '')

    def test_dollar_quoted_content_and_offsets_are_literal(self):
        bodies = ["a''b; $1", r"a\b", "INTERVAL '-1 DAY' CASE WHEN x THEN 0END", "'"]
        for delimiter in ('$$', '$tag$', '$_tag_1$', '$中文$'):
            for body in bodies:
                sql = 'SELECT $1, ' + delimiter + body + delimiter
                with self.subTest(delimiter=delimiter, body=body):
                    for dialect in (LexicalPostgres, CompatiblePostgres, _AuditPostgres):
                        token = dialect().tokenize(sql)[-1]
                        self.assertEqual(token.token_type, TokenType.HEREDOC_STRING)
                        self.assertEqual(token.text, body)
                        self.assertEqual(sql[token.start:token.end + 1], delimiter + body + delimiter)
                    self.assertEqual(prepare_sql(sql)[0], sql)
                    self.assertEqual(compatible_postgres(sql), sql)

    def test_regular_and_escape_string_rules_are_unchanged(self):
        for sql in ("SELECT 'a''b'", r"SELECT 'a\b'", r"SELECT E'a\nb'",
                    'SELECT "quoted;name"', "SELECT /* outer /* inner */ end */ ''"):
            with self.subTest(sql=sql):
                expected = [(t.token_type, t.text, t.start, t.end) for t in Postgres().tokenize(sql)]
                self.assertEqual([(t.token_type, t.text, t.start, t.end)
                                  for t in tokenize_postgres(sql)], expected)

    def test_unclosed_tokens_and_numeric_dollar_quotes_remain_errors(self):
        for sql in ("SELECT 'unterminated", 'SELECT "unterminated',
                    "SELECT $$unterminated", 'SELECT $tag$unterminated',
                    "SELECT $1, 'unterminated", 'SELECT $1 /* open /* nested */',
                    'SELECT $1$body$1$', "SELECT $1$body$1$, ''"):
            with self.subTest(sql=sql):
                for dialect in (LexicalPostgres, CompatiblePostgres, _AuditPostgres):
                    with self.assertRaises(TokenError):
                        dialect().tokenize(sql)
        # A numeric prefix must never be interpreted as a dollar-quote tag.
        tokens = tokenize_postgres('SELECT $9_a$body$9_a$')
        self.assertFalse(any(t.token_type == TokenType.HEREDOC_STRING for t in tokens))

    def test_split_retains_semicolons_in_strings_comments_and_dollar_quotes(self):
        first = "SELECT $1, $tag$'';\\;$tag$, ''"
        second = "SELECT $2, ''"
        sql = first + '; /* outer /* inner; */ end */ ' + second
        expected = [first, '/* outer /* inner; */ end */ ' + second]
        self.assertEqual(split_sql_statements(sql), expected)
        self.assertEqual(_split(sql), expected)
        self.assertEqual(split_sql_statements("SELECT $1, 'unclosed"), ["SELECT $1, 'unclosed"])

    def test_preprocessing_repairs_actual_tokens_without_touching_dollar_text(self):
        sql = "SELECT CASE WHEN TRUE THEN 0END, $1, ''"
        prepared, notes = prepare_sql(sql)
        self.assertIn('0 END', prepared)
        self.assertTrue(notes)
        self.assertIsInstance(parse_one_postgres(sql), exp.Select)
        dollar = "SELECT $1, $$CASE WHEN TRUE THEN 0END; INTERVAL '-1 DAY'$$, ''"
        self.assertEqual(prepare_sql(dollar)[0], dollar)

    def test_ddl_copy_and_following_parameter_statement_are_separate(self):
        statements = ['ALTER TABLE s.events ADD PARTITION p1',
                      'COPY s.events(data_dt) TO STDOUT',
                      "SELECT * FROM s.events WHERE data_dt=$1 AND marker!=''"]
        self.assertEqual(split_sql_statements('; '.join(statements)), statements)
        self.assertEqual(parse_alter(statements[0])[0].sql(dialect='postgres'), 's.events')
        table, direction, incomplete = recover_copy_header(statements[1])
        self.assertEqual((table.sql(dialect='postgres'), direction, incomplete),
                         ('s.events', 'COPY读取', False))
        source, analysis, report = self.analyze('; '.join(statements))
        self.assertEqual(analysis['index'][0]['status'], '已解析')
        self.assertEqual({r['occurrence'] for r in analysis['details']}, {1, 2, 3})
        self.assertFalse(any(r['partition_identity'][0] == 'values' for r in report['mapping']))
        self.assertFalse(check_partition_omissions(source, report)['alerts'])

    def test_dynamic_parameter_is_not_a_definite_partition(self):
        for sql in ("SELECT * FROM s.events WHERE data_dt=$1 AND marker!=''",
                    "INSERT INTO s.events(data_dt,marker) VALUES ($1,'')",
                    "UPDATE s.events SET data_dt=$1,marker='' WHERE id=1"):
            with self.subTest(sql=sql):
                source, analysis, report = self.analyze(sql)
                self.assertEqual(analysis['index'][0]['status'], '已解析')
                self.assertTrue(report['mapping'])
                self.assertFalse(any(r['partition_identity'][0] == 'values' for r in report['mapping']))
                checked = check_partition_omissions(source, report)
                self.assertEqual(checked['metrics']['expected_accesses'], 0)
                self.assertFalse(checked['alerts'])

    def test_independent_checker_still_detects_definite_partition_with_parameter_elsewhere(self):
        sql = "SELECT $1 FROM s.events WHERE data_dt='a' AND marker!=''"
        source, analysis, report = self.analyze(sql)
        self.assertEqual(analysis['index'][0]['status'], '已解析')
        self.assertEqual(report['mapping'][0]['partition_identity'], ('values', (('data_dt', 'string', 'a'),)))
        with patch('sql_partition_analysis.partitioning.lexical.tokenize_postgres',
                   side_effect=AssertionError('checker must use its own tokenizer')):
            self.assertFalse(check_partition_omissions(source, report)['alerts'])
            missing = dict(report, mapping=[], results=[])
            alert, = check_partition_omissions(source, missing)['alerts']
        self.assertEqual(alert['partition_value'], {'type': 'string', 'value': 'a'})

    def test_original_four_parameter_queries_parse_without_inventing_schema(self):
        empty = {'version': 1, 'tables': {}}
        for sid, sql in ORIGINAL_PARAMETER_SQL:
            with self.subTest(sql_id=sid):
                source, analysis, report = self.analyze(sql, empty)
                self.assertEqual(source['sqls'][0]['sql'], sql)
                self.assertEqual(analysis['index'][0]['status'], '已解析')
                self.assertEqual(len(analysis['details']), 1)
                self.assertTrue(analysis['details'][0]['table'].startswith('[未指定schema].'))
                self.assertFalse(report['mapping'])
                tree = parse_one_postgres(sql.replace('\x18', '\n'))
                parameter, = tree.find_all(exp.Parameter)
                self.assertEqual(parameter.this.this, '1')


if __name__ == '__main__':
    unittest.main()
