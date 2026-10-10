"""Source-preserving compatibility and counterexamples at the parser boundary."""
import _support
import ast
from pathlib import Path
import sys
import unittest

from sqlglot import exp
from sqlglot.errors import ParseError
from sql_partition_analysis.partitioning.parser import (
    prepare_sql, parse_postgres, parse_one_postgres, sql_start_keyword,
    recover_identifier_quote_encoding,
)


class ParserPreprocessingTests(unittest.TestCase):
    def test_distribution_is_per_create_statement_and_retains_comments(self):
        text = (
            "-- ALTER TABLE dummy SET DISTRIBUTED BY (old)\x18"
            "CREATE TEMP TABLE target AS SELECT * FROM s.source "
            "DISTRIBUTED /* placement */ BY (id); "
            "SELECT 'DISTRIBUTED BY (literal)' FROM s.later; "
            "CREATE TABLE next_target AS (SELECT * FROM s.next_source) "
            "DISTRIBUTED REPLICATED; -- DISTRIBUTED RANDOMLY"
        )
        prepared, notes = prepare_sql(text)
        self.assertIn("-- ALTER TABLE dummy SET DISTRIBUTED BY (old)\n", prepared)
        self.assertIn("/* placement */", prepared)
        self.assertIn("'DISTRIBUTED BY (literal)'", prepared)
        self.assertIn("-- DISTRIBUTED RANDOMLY", prepared)
        trees = [tree for tree in parse_postgres(prepared) if isinstance(tree, (exp.Create, exp.Select))]
        self.assertEqual(len(trees), 3)
        self.assertEqual([table.name for tree in trees for table in tree.find_all(exp.Table)],
                         ["target", "source", "later", "next_target", "next_source"])
        self.assertTrue(any("DISTRIBUTED" in note for note in notes))
        self.assertEqual(prepare_sql(prepared), (prepared, []))

    def test_distribution_variants_have_the_same_select_ast(self):
        base = "CREATE TABLE t AS SELECT * FROM s.events WHERE data_dt=DATE '20261007'"
        expected = parse_one_postgres(base).sql(comments=False)
        for suffix in ("DISTRIBUTED BY (id)", 'DISTRIBUTED BY ("id", data_dt)',
                       "DISTRIBUTED BY (id int4_ops, data_dt pg_catalog.date_ops)",
                       "DISTRIBUTED RANDOMLY", "DISTRIBUTED REPLICATED"):
            with self.subTest(suffix=suffix):
                tree = parse_one_postgres(base + " " + suffix + ";")
                self.assertIsInstance(tree, exp.Create)
                self.assertEqual(tree.sql(comments=False), expected)

    def test_distribution_like_text_is_not_removed(self):
        cases = [
            "SELECT * FROM t DISTRIBUTED BY (id)",
            "ALTER TABLE t SET DISTRIBUTED BY (id)",
            "CREATE VIEW t AS SELECT * FROM s.t DISTRIBUTED RANDOMLY",
            "CREATE TABLE t AS SELECT 'DISTRIBUTED BY (id)'",
            'CREATE TABLE t AS SELECT "DISTRIBUTED", "REPLICATED"',
            "CREATE TABLE t AS SELECT * FROM s.t -- DISTRIBUTED REPLICATED",
            "CREATE TABLE t AS (SELECT * FROM s.t DISTRIBUTED REPLICATED)",
            "CREATE TABLE t AS SELECT * FROM s.t DISTRIBUTED BY (id) trailing",
            "CREATE TABLE t AS SELECT * FROM s.t DISTRIBUTED BY (func(id))",
            "CREATE TABLE t AS SELECT * FROM s.t DISTRIBUTED BY (id",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(prepare_sql(text), (text, []))

    def test_distribution_words_in_complete_postgres_create_are_preserved(self):
        cases = [
            "CREATE TABLE audit.target AS SELECT * FROM audit.distributed randomly",
            "CREATE TABLE audit.target AS SELECT * FROM distributed randomly",
            "CREATE TABLE audit.target AS SELECT distributed randomly",
            "CREATE TABLE audit.target AS SELECT 1, distributed randomly",
            'CREATE TABLE audit.target AS SELECT * FROM audit."distributed" randomly',
            'CREATE TABLE audit.target AS SELECT "distributed" AS "randomly"',
            "CREATE TABLE audit.target AS SELECT * FROM audit.distributed /* alias */ randomly",
            "CREATE TABLE audit.target AS SELECT * FROM audit.distributed replicated",
            "CREATE TABLE audit.target AS SELECT distributed replicated -- normal projection",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(prepare_sql(text), (text, []))
                tree = parse_one_postgres(text)
                self.assertIsInstance(tree, exp.Create)
                self.assertTrue(tree.expression.expressions)

    def test_distribution_removal_requires_a_complete_create_body(self):
        for text in (
                "CREATE TABLE t AS SELECT DISTRIBUTED RANDOMLY",
                "CREATE TABLE t AS SELECT 1, DISTRIBUTED RANDOMLY",
                "CREATE TABLE t DISTRIBUTED RANDOMLY",
                "CREATE TABLE t AS DISTRIBUTED REPLICATED",
                "CREATE TABLE t AS SELECT * FROM DISTRIBUTED REPLICATED",
                "CREATE TABLE t AS SELECT * FROM s. DISTRIBUTED REPLICATED"):
            with self.subTest(text=text):
                self.assertEqual(prepare_sql(text), (text, []))
        text = ("CREATE TABLE t AS SELECT * FROM audit.distributed randomly "
                "/* table alias already complete */ DISTRIBUTED -- placement\n REPLICATED")
        prepared, notes = prepare_sql(text)
        self.assertIn("audit.distributed randomly", prepared)
        self.assertIn("/* table alias already complete */", prepared)
        self.assertIn("-- placement\n", prepared)
        self.assertTrue(notes)
        tree = parse_one_postgres(text)
        self.assertEqual(tree.expression.args["from"].this.name, "distributed")
        self.assertEqual(tree.expression.args["from"].this.alias, "randomly")
        definition = parse_one_postgres("CREATE TABLE t (id INT) /* complete schema */ DISTRIBUTED BY (id)")
        self.assertIsInstance(definition.this, exp.Schema)
        compatibility = parse_one_postgres(
            "CREATE TABLE t AS SELECT DATE '20261007'::DATE, "
            "CASE WHEN x=1 THEN 1 ELSE 0END FROM s.events DISTRIBUTED RANDOMLY")
        self.assertEqual(compatibility.find(exp.Case).args["default"].this, "0")
        self.assertEqual([t.name for t in compatibility.find_all(exp.Table)], ["t", "events"])

    def test_values_identifiers_keep_case_normalization_and_aliases(self):
        statement = (
            "INSERT INTO s.target SELECT CASE WHEN values IS NULL THEN NULL "
            "ELSE values END values FROM s.source values "
            "WHERE values.create_date=DATE '20261007'"
        )
        self.assertEqual(prepare_sql(statement), (statement, []))
        tree = parse_one_postgres(statement)
        self.assertIsInstance(tree, exp.Insert)
        select = tree.expression
        self.assertEqual(select.expressions[0].alias, "values")
        self.assertEqual(select.args["from"].this.alias, "values")
        self.assertEqual([t.name for t in tree.find_all(exp.Table)], ["target", "source"])

    def test_actual_values_and_insert_values_are_unchanged(self):
        for text in ("VALUES (1), (2)", "INSERT INTO t(values) VALUES (1), (2)",
                     "SELECT * FROM (VALUES (1), (2)) v(values)",
                     "WITH v(x) AS (VALUES (1)) SELECT x FROM v",
                     "SELECT values FROM s.t WHERE values=1"):
            with self.subTest(text=text):
                tree = parse_one_postgres(text)
                self.assertTrue(tree.find(exp.Values) or isinstance(tree, exp.Select))
        self.assertIsInstance(parse_one_postgres("VALUES (1)"), exp.Values)

    def test_extra_function_arguments_survive_without_dropping_source_tables(self):
        calls = [
            "DATE_TRUNC('day', TIMESTAMP '2026-10-07 00:00:00', 'Australia/Sydney')",
            "TO_DATE('20261007', 'YYYYMMDD', TRUE)",
            "TO_TIMESTAMP('20261007', 'YYYYMMDD', TRUE)",
        ]
        for call in calls:
            with self.subTest(call=call):
                tree = parse_one_postgres("SELECT * FROM s.events WHERE data_dt=" + call)
                function = tree.find(exp.Anonymous)
                self.assertIsNotNone(function)
                self.assertEqual(len(function.expressions), 3)
                self.assertEqual([t.name for t in tree.find_all(exp.Table)], ["events"])
                self.assertIn(function.expressions[-1].sql(), tree.sql())
        for call, node_type in (
                ("DATE_TRUNC('day', TIMESTAMP '2026-10-07 00:00:00')", exp.TimestampTrunc),
                ("TO_DATE('20261007', 'YYYYMMDD')", exp.StrToDate),
                ("TO_TIMESTAMP('20261007', 'YYYYMMDD')", exp.StrToTime)):
            with self.subTest(call=call):
                self.assertIsNotNone(parse_one_postgres("SELECT " + call).find(node_type))

    def test_postgres_templates_preserve_percent_literals_before_format_conversion(self):
        from sql_partition_analysis.partitioning.constants import value
        for name, node_type in (("TO_DATE", exp.StrToDate), ("TO_TIMESTAMP", exp.StrToTime)):
            with self.subTest(name=name):
                pg_template = parse_one_postgres("SELECT " + name + "('20260901', 'YYYYMMDD')")
                percent_template = parse_one_postgres("SELECT " + name + "('20260901', '%Y%m%d')")
                self.assertIsNotNone(pg_template.find(node_type))
                opaque = percent_template.find(exp.Anonymous)
                self.assertIsNotNone(opaque)
                self.assertEqual(opaque.expressions[1].this, "%Y%m%d")
                self.assertIsNone(value(opaque))
                self.assertIn("'%Y%m%d'", percent_template.sql(dialect="postgres"))
                self.assertIn("'YYYYMMDD'", pg_template.sql(dialect="postgres"))
                self.assertNotEqual(pg_template.sql(dialect="postgres"),
                                    percent_template.sql(dialect="postgres"))
        normal = parse_one_postgres("SELECT TO_DATE('20260901', 'YYYYMMDD')").find(exp.StrToDate)
        self.assertEqual(value(normal).isoformat(), "2026-09-01")
        statement = "SELECT * FROM s.events WHERE data_dt=TO_DATE('20260901', '%Y%m%d')"
        self.assertEqual(prepare_sql(statement), (statement, []))
        self.assertEqual([table.name for table in parse_one_postgres(statement).find_all(exp.Table)], ["events"])

    def test_zero_end_is_only_a_case_result_boundary(self):
        text = "SELECT CASE WHEN a=1 THEN 1 ELSE 0END AS x FROM s.t"
        prepared, notes = prepare_sql(text)
        self.assertIn("ELSE 0 END AS x", prepared)
        self.assertEqual(parse_one_postgres(text).find(exp.Case).args["default"].this, "0")
        self.assertTrue(notes)
        for unchanged in (
                "SELECT '0END', $$0END$$, \"0END\" FROM s.t -- 0END",
                "SELECT 0END FROM s.t", "SELECT * FROM s.t WHERE x=0END",
                "SELECT CASE WHEN x=0END THEN 1 ELSE 0 END FROM s.t",
                "SELECT CASE WHEN x=1 THEN 0E'ND' ELSE 1 END FROM s.t",
                "SELECT CASE WHEN x=1 THEN 0ENDless ELSE 1 END FROM s.t"):
            self.assertEqual(prepare_sql(unchanged), (unchanged, []))

    def test_nonstandard_case_is_preserved_without_null_semantics_guess(self):
        text = "SELECT CASE x WHEN IS NOT DISTINCT FROM NULL THEN 1 ELSE 0 END FROM s.t"
        prepared, notes = prepare_sql(text)
        self.assertEqual(prepared, text)
        self.assertTrue(any("未猜测" in note for note in notes))
        with self.assertRaises(ParseError):
            parse_one_postgres(text)
        valid = "SELECT * FROM s.t WHERE x IS NOT DISTINCT FROM NULL"
        self.assertEqual(prepare_sql(valid), (valid, []))
        self.assertIsInstance(parse_one_postgres(valid), exp.Select)

    def test_start_detection_protects_literals_and_truncated_copy(self):
        self.assertEqual(sql_start_keyword("/* a /* nested */ comment */ -- x\x18COPY s.t (\"truncated"), "COPY")
        self.assertEqual(sql_start_keyword("-- DISTRIBUTED BY (id)\x18SELECT * FROM s.t"), "SELECT")
        self.assertEqual(sql_start_keyword("VALUES (1)"), "VALUES")
        for text in ("-- SELECT * FROM s.t", "/* SELECT */", '"SELECT"',
                     "'SELECT * FROM s.t'", "$$SELECT * FROM s.t$$",
                     "An exception was encountered: SELECT * FROM s.t", "/* unfinished SELECT", "SELECT$log"):
            self.assertIsNone(sql_start_keyword(text), text)

    def test_quote_recovery_restores_only_complete_identifier_frames(self):
        text = 'COPY ""bohzedw"".""s"".""t"" (""id"",""row_date"") FROM STDIN WITH CSV'
        with self.assertRaises(ParseError):
            parse_one_postgres(text)
        recovered = recover_identifier_quote_encoding(text)
        self.assertEqual(recovered, 'COPY "bohzedw"."s"."t" ("id","row_date") FROM STDIN WITH CSV')
        tree = parse_one_postgres(recovered)
        self.assertIsInstance(tree, exp.Copy)
        self.assertTrue(tree.args["kind"])
        self.assertEqual(tree.this.this.name, "t")

    def test_copy_options_and_escaped_string_payload_survive_quote_recovery(self):
        text = ('COPY ""s"".""t"" (""id"") FROM STDIN '
                "DELIMITER '|' ESCAPE '\\' CSV QUOTE '\"\"'")
        recovered = recover_identifier_quote_encoding(text)
        self.assertEqual(recovered, text.replace('""s"".""t"" (""id"")', '"s"."t" ("id")'))
        self.assertIsInstance(parse_one_postgres(recovered), exp.Copy)
        escaped = "SELECT E'it\\'s \"\"safe\"\"' FROM \"\"t\"\""
        self.assertEqual(recover_identifier_quote_encoding(escaped), escaped.replace('""t""', '"t"'))

    def test_quote_recovery_preserves_strings_comments_and_legal_identifier_quotes(self):
        text = ('SELECT "a""b", \'""literal""\', $tag$""dollar""$tag$, $标签$""unicode""$标签$ '
                'FROM ""s"".""t"" /* ""comment"" /* nested */ */ -- ""line""')
        recovered = recover_identifier_quote_encoding(text)
        self.assertEqual(recovered, text.replace('""s"".""t""', '"s"."t"'))
        tree = parse_one_postgres(recovered)
        self.assertEqual(tree.expressions[0].name, 'a"b')
        self.assertEqual(tree.expressions[1].this, '""literal""')
        self.assertEqual(tree.expressions[2].this, '""dollar""')

    def test_quote_recovery_accepts_complete_quoted_identifier_contents(self):
        text = ('SELECT amount ""日期时间(发送日期)"", flag ""签约类型（分为大众版、专业版）"", '
                '""122_index_name"", ""two words / punctuation.!%$\'ok\' /* not comment */"" '
                'FROM ""s"".""a table-name""')
        with self.assertRaises(ParseError):
            parse_one_postgres(text)
        recovered = recover_identifier_quote_encoding(text)
        self.assertIsNotNone(recovered)
        tree = parse_one_postgres(recovered)
        self.assertEqual(tree.expressions[0].alias, "日期时间(发送日期)")
        self.assertEqual(tree.expressions[1].alias, "签约类型（分为大众版、专业版）")
        self.assertEqual(tree.expressions[2].name, "122_index_name")
        self.assertEqual(tree.expressions[3].name, "two words / punctuation.!%$'ok' /* not comment */")
        self.assertEqual(tree.find(exp.Table).name, "a table-name")
        text = 'SET idle_session_timeout=""2h""; SELECT ""122_index_name"" FROM s.events'
        recovered = recover_identifier_quote_encoding(text)
        self.assertEqual(recovered, 'SET idle_session_timeout="2h"; SELECT "122_index_name" FROM s.events')
        trees = [tree for tree in parse_postgres(recovered) if tree is not None]
        self.assertEqual(len(trees), 2)
        self.assertIsInstance(trees[0], exp.Set)
        self.assertEqual(trees[1].find(exp.Table).name, "events")

    def test_quote_recovery_rejects_incomplete_mixed_and_ambiguous_frames(self):
        for text in ('SELECT "a""b" FROM s.t', 'COPY ""s"".""t" (""id"") FROM STDIN',
                     'SELECT ""one"", ""two" FROM ""t""',
                     'SELECT ""a""""b"" FROM ""t""', 'SELECT ""bad\x00name"" FROM ""t""',
                     'SELECT x""id"" FROM ""t""', 'SELECT ""id""suffix FROM ""t""',
                     'SELECT \'unfinished FROM ""t""', 'SELECT $$unfinished FROM ""t""',
                     'SELECT ""id"" FROM ""t"" /* unfinished'):
            with self.subTest(text=text):
                self.assertIsNone(recover_identifier_quote_encoding(text))

    def test_source_is_python37_compatible(self):
        root = Path(__file__).resolve().parents[1] / "src/sql_partition_analysis/partitioning"
        for name in ("parser.py", "preprocessing.py"):
            source = (root / name).read_text(encoding='utf-8')
            if sys.version_info[:2] == (3, 7):
                # Python 3.7 itself is the grammar check; feature_version was
                # introduced later and must not prevent native 3.7 test runs.
                ast.parse(source)
            else:
                ast.parse(source, feature_version=(3, 7))


if __name__ == "__main__":
    unittest.main()
