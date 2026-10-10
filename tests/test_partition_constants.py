"""Type and formatting regressions for conservative partition constants."""

import _support
import datetime
from decimal import Decimal
import unittest

from sql_partition_analysis.partitioning.constants import (
    Interval, normalize_expression, render_value, value,
)
from sql_partition_analysis.partitioning.parser import parse_one_postgres


class PartitionConstantsTests(unittest.TestCase):
    def expression(self, text):
        return parse_one_postgres("SELECT " + text).expressions[0]

    def evaluate(self, text):
        return value(self.expression(text))

    def test_bare_date_like_values_keep_their_types_and_spelling(self):
        for text, expected in [("'20260901'", "20260901"), ("20260901", 20260901),
                               ("'2026-09-01'", "2026-09-01"),
                               ("'2026-09-01 12:34:56'", "2026-09-01 12:34:56"),
                               ("1.25", Decimal("1.25")), ("-1", -1)]:
            with self.subTest(text=text):
                node = self.expression(text)
                self.assertEqual(value(node), expected)
                self.assertIs(type(value(node)), type(expected))
                self.assertEqual(normalize_expression(node).sql(), node.sql())

    def test_negative_decimal_preserves_all_literal_digits(self):
        text = '-123456789012345678901234567890.123'
        self.assertEqual(self.evaluate(text), Decimal(text))
        self.assertEqual(render_value(self.evaluate(text)), text)

    def test_explicit_date_and_integer_arithmetic(self):
        for text in ("DATE '20260901'", "CAST('2026-09-01' AS DATE)", "DATE('20260901')"):
            self.assertEqual(self.evaluate(text), datetime.date(2026, 9, 1))
        self.assertEqual(self.evaluate("DATE '20260901' - 1"), datetime.date(2026, 8, 31))
        self.assertEqual(self.evaluate("DATE '20260901' + -1"), datetime.date(2026, 8, 31))
        self.assertEqual(self.evaluate("1 + DATE '20260901'"), datetime.date(2026, 9, 2))
        self.assertEqual(self.evaluate("DATE '20260902' - DATE '20260901'"), 1)
        self.assertIsNone(self.evaluate("CAST(20260901 AS DATE)"))

    def test_to_date_obeys_its_template(self):
        self.assertEqual(self.evaluate("TO_DATE('01/09/2026', 'DD/MM/YYYY')"), datetime.date(2026, 9, 1))
        self.assertEqual(self.evaluate("TO_DATE('2026-01-09', 'YYYY-DD-MM')"), datetime.date(2026, 9, 1))
        self.assertEqual(self.evaluate("TO_DATE('20260901', 'YYYYMMDD')"), datetime.date(2026, 9, 1))
        self.assertEqual(self.evaluate("TO_DATE(CAST(20260901 AS VARCHAR), 'YYYYMMDD')"), datetime.date(2026, 9, 1))
        self.assertEqual(self.evaluate("TO_DATE('2026-09-01 12:34:56', 'YYYY-MM-DD HH24:MI:SS')"), datetime.date(2026, 9, 1))
        self.assertEqual(self.evaluate("TO_DATE('2026-09-01 00:00:00', 'YYYY-MM-DD')"), datetime.date(2026, 9, 1))
        for text in ("TO_DATE('20260901', 'DDMMYYYY')", "TO_DATE('20260901', 'unknown')",
                     "TO_DATE('2026-09-01', 'YYYY-MM')", "TO_DATE('2026-SEP-01', 'YYYY-MON-DD')",
                     "TO_DATE('20260901')", "TO_DATE('20260901', format_column)"):
            with self.subTest(text=text):
                self.assertIsNone(self.evaluate(text))

    def test_timestamp_keeps_time_and_explicit_date_cast_drops_it(self):
        text = "TIMESTAMP '2026-09-01 12:34:56.123456'"
        expected = datetime.datetime(2026, 9, 1, 12, 34, 56, 123456)
        self.assertEqual(self.evaluate(text), expected)
        self.assertEqual(render_value(expected), "2026-09-01 12:34:56.123456")
        node = normalize_expression(self.expression(text))
        self.assertEqual(value(node), expected)
        self.assertIn("TIMESTAMP", node.sql(dialect="postgres"))
        self.assertEqual(self.evaluate("CAST(" + text + " AS DATE)"), datetime.date(2026, 9, 1))
        self.assertIsNone(self.evaluate("CAST('2026-09-01 12:34:56.6' AS TIMESTAMP(0))"))

    def test_to_date_accepts_exact_lowercase_and_mixed_token_spellings(self):
        examples = [("20261007", "yyyymmdd"), ("2026-10-07", "yyyy-MM-dd"),
                    ("20261007", "yyyyMMDD"), ("20261007", "YYYYmmDD"),
                    ("07/10/2026", "DD/mm/yyyy"), ("07102026", "ddmmyyyy"),
                    ("10072026", "mmddyyyy"), ("2026-07-10", "yyyy-dd-MM"),
                    ("2026-10-07 12:34:56", "yyyy-mm-dd hh24:mi:ss")]
        for text, template in examples:
            with self.subTest(text=text, template=template):
                node = self.expression("TO_DATE('{}', '{}')".format(text, template))
                self.assertEqual(value(node), datetime.date(2026, 10, 7))
                self.assertEqual(value(normalize_expression(node)), datetime.date(2026, 10, 7))

    def test_to_date_does_not_casefold_unknown_or_partial_tokens(self):
        for template in ("YyYy-Mm-Dd", "yyyy-Mm-dd", "yyyy-mon-dd", "yyyy-mm",
                         "fxyyyy-mm-dd", "FMDD/mm/yyyy", "unknown", "YYYYYYmmdd"):
            with self.subTest(template=template):
                node = self.expression("TO_DATE('2026-10-07', '{}')".format(template))
                self.assertIsNone(value(node))
                self.assertEqual(normalize_expression(node).sql(), node.sql())

    def test_session_dependent_casts_and_timezones_are_unknown(self):
        for text in ("CAST('09/01/2026' AS DATE)", "CAST(DATE '20260901' AS TEXT)",
                     "CAST(TIMESTAMP '2026-09-01 12:34:56' AS VARCHAR)",
                     "TIMESTAMPTZ '2026-09-01 12:34:56+08'",
                     "DATE('20260901', 'Asia/Shanghai')",
                     "TO_TIMESTAMP('2026-09-01 12:34:56', 'YYYY-MM-DD HH24:MI:SS')",
                     "CAST('2026-09-01 12:34:56+08' AS TIMESTAMP)"):
            with self.subTest(text=text):
                node = self.expression(text)
                self.assertIsNone(value(node))
                self.assertEqual(normalize_expression(node).sql(), node.sql())

    def test_truncation_resets_time_and_preserves_timestamp_result_type(self):
        for unit, expected in [("day", datetime.datetime(2026, 9, 3)),
                               ("month", datetime.datetime(2026, 9, 1)),
                               ("year", datetime.datetime(2026, 1, 1)),
                               ("week", datetime.datetime(2026, 8, 31))]:
            text = "DATE_TRUNC('{}', TIMESTAMP '2026-09-03 12:34:56.123')".format(unit)
            with self.subTest(unit=unit):
                self.assertEqual(self.evaluate(text), expected)
                self.assertIs(type(self.evaluate(text)), datetime.datetime)
        self.assertEqual(self.evaluate("DATE_TRUNC('day', CAST(DATE '20260903' AS TIMESTAMP))"), datetime.datetime(2026, 9, 3))
        self.assertIsNone(self.evaluate("DATE_TRUNC('day', '20260903')"))

    def test_date_trunc_date_keeps_session_timezone_dependent_overload(self):
        for text in ("DATE_TRUNC('day', DATE '20260903')",
                     "DATE_TRUNC('month', TO_DATE('20260903', 'YYYYMMDD'))",
                     "CAST(DATE_TRUNC('year', DATE '20260903') AS DATE)",
                     "CAST(DATE_TRUNC('month', DATE '20260903') + '-1 DAY' AS DATE)"):
            with self.subTest(text=text):
                node = self.expression(text)
                self.assertIsNone(value(node))
                self.assertEqual(normalize_expression(node).sql(), node.sql())

    def test_signed_intervals_and_month_end_keep_postgres_semantics(self):
        self.assertEqual(self.evaluate("INTERVAL '-1 month'"), Interval(-1, 0, 0))
        self.assertEqual(self.evaluate("DATE '2026-03-31' + INTERVAL '-1 month'"), datetime.datetime(2026, 2, 28))
        self.assertEqual(self.evaluate("DATE '2024-02-29' + INTERVAL '1 year'"), datetime.datetime(2025, 2, 28))
        self.assertEqual(self.evaluate("DATE '2026-09-01' + INTERVAL '2 hours'"), datetime.datetime(2026, 9, 1, 2))
        self.assertEqual(self.evaluate("TIMESTAMP '2026-09-01 12:34:56' - INTERVAL '1 day'"), datetime.datetime(2026, 8, 31, 12, 34, 56))
        self.assertEqual(self.evaluate("DATE '2026-06-30' + INTERVAL '1 DAY' + INTERVAL '-1 MONTH' + INTERVAL '-1 DAY'"), datetime.datetime(2026, 5, 31))
        self.assertEqual(self.evaluate("CAST(DATE_TRUNC('month', CAST(DATE '20260903' AS TIMESTAMP)) + '-1 DAY' AS DATE)"), datetime.date(2026, 8, 31))
        self.assertIsNone(self.evaluate("TIMESTAMP '2026-09-01 12:34:56' + 1"))

    def test_unevaluated_scalar_is_a_normalization_barrier(self):
        for text in ("TO_CHAR(DATE '20260901', 'YYYYMMDD')",
                     "TO_DATE(CAST(DATE '20260901' AS TEXT), 'YYYYMMDD')",
                     "COALESCE(DATE '20260901', CURRENT_DATE)",
                     "DATE '20260901' + dynamic_interval",
                     "CAST(DATE '20260901' AS VARCHAR(5))"):
            with self.subTest(text=text):
                node = self.expression(text)
                self.assertIsNone(value(node))
                self.assertEqual(normalize_expression(node).sql(), node.sql())

    def test_predicate_folds_typed_values_without_mutating_input(self):
        node = self.expression("data_dt = DATE '20260901' - 1 AND code = '20260901'")
        original = node.sql()
        normalized = normalize_expression(node)
        self.assertEqual(node.sql(), original)
        self.assertIsNot(normalized, node)
        self.assertIn("CAST('2026-08-31' AS DATE)", normalized.sql(dialect="postgres"))
        self.assertIn("code = '20260901'", normalized.sql(dialect="postgres"))

    def test_explicit_date_common_type_in_list_avoids_false_distinct_values(self):
        node = self.expression("data_dt IN ('20260901', DATE '2026-09-01')")
        normalized = normalize_expression(node)
        self.assertEqual([value(member) for member in normalized.expressions],
                         [datetime.date(2026, 9, 1), datetime.date(2026, 9, 1)])
        self.assertEqual(node.expressions[0].this, "20260901")
        bare = self.expression("data_dt IN ('20260901', '2026-09-01')")
        self.assertEqual([value(member) for member in normalize_expression(bare).expressions],
                         ["20260901", "2026-09-01"])
        ambiguous = self.expression("data_dt IN ('09/01/2026', DATE '2026-09-01')")
        self.assertEqual(normalize_expression(ambiguous).expressions[0].this, "09/01/2026")

    def test_invalid_or_dynamic_constants_cannot_become_values(self):
        for text in ("DATE '20260230'", "DATE '2026-13-01'", "CURRENT_DATE",
                     "DATE '20260901' + $1", "2147483647 + 1",
                     "CAST('32768' AS SMALLINT)", "CAST('abc' AS INT)"):
            with self.subTest(text=text):
                self.assertIsNone(self.evaluate(text))


if __name__ == "__main__":
    unittest.main()
