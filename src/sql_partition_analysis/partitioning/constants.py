"""Conservative PostgreSQL constant evaluation without column or session types.

An untyped string remains a string, even when it resembles a date.  Unsupported
casts, formatting templates and session-dependent operations return ``None``.
Normalization folds complete temporal expressions into *typed* constants; an
unevaluated scalar subtree is left intact, including its constant children.
"""

import calendar
import datetime
import re
from collections import namedtuple
from decimal import Decimal, InvalidOperation

from sqlglot import exp


Interval = namedtuple("Interval", "months days microseconds")
_DATE = re.compile(r"(?:\d{4}-\d{2}-\d{2}|\d{8})\Z")
_TIMESTAMP = re.compile(
    r"(\d{4}-\d{2}-\d{2}|\d{8})[ T](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,6}))?)?\Z"
)
_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\Z")
_INTERVAL = re.compile(
    r"\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*"
    r"(years?|months?|weeks?|days?|hours?|minutes?|seconds?|milliseconds?|microseconds?)\s*\Z",
    re.IGNORECASE,
)
# PostgreSQL formatting.c's DCH_keywords/index_seq_search accepts these exact
# upper/lower spellings; it does not casefold arbitrary mixed-case tokens.
# SQLGlot converts only some spellings to strptime directives, so its AST can
# contain a mixture, for example "yyyy-%m-dd" for "yyyy-MM-dd".
_DATE_FORMAT_TOKENS = {"YYYY": "%Y", "yyyy": "%Y", "MM": "%m", "mm": "%m",
                       "DD": "%d", "dd": "%d", "HH24": "%H", "hh24": "%H",
                       "MI": "%M", "mi": "%M", "SS": "%S", "ss": "%S"}
_DATE_FORMAT_TOKEN = re.compile(
    r"%[YmdHMS]|YYYY|yyyy|HH24|hh24|MM|mm|DD|dd|MI|mi|SS|ss|[-/:. \t]"
)


def _date_template(template):
    pieces = []
    position = 0
    while position < len(template):
        match = _DATE_FORMAT_TOKEN.match(template, position)
        if match is None:
            return None
        token = match[0]
        pieces.append(_DATE_FORMAT_TOKENS.get(token, token))
        position = match.end()
    return "".join(pieces)


def _date(text):
    if not isinstance(text, str) or not _DATE.fullmatch(text):
        return None
    try:
        return datetime.datetime.strptime(text, "%Y-%m-%d" if "-" in text else "%Y%m%d").date()
    except ValueError:
        return None


def _timestamp(text):
    if not isinstance(text, str):
        return None
    date = _date(text)
    if date is not None:
        return datetime.datetime.combine(date, datetime.time())
    match = _TIMESTAMP.fullmatch(text)
    if match is None:
        return None
    date = _date(match[1])
    if date is None:
        return None
    try:
        return datetime.datetime.combine(date, datetime.time(
            int(match[2]), int(match[3]), int(match[4] or 0),
            int((match[5] or "").ljust(6, "0") or 0)))
    except ValueError:
        return None


def _as_date(item):
    if isinstance(item, datetime.datetime):
        return item.date() if item.tzinfo is None else None
    if isinstance(item, datetime.date):
        return item
    if isinstance(item, str):
        result = _date(item)
        if result is not None:
            return result
        timestamp = _timestamp(item)
        return timestamp.date() if timestamp is not None else None
    return None


def _format_date(text, template):
    """Evaluate an exact numeric subset of PostgreSQL's formatting templates."""
    if not isinstance(text, str) or not isinstance(template, str):
        return None
    template = _date_template(template)
    if template is None:
        return None
    directives = re.findall(r"%.", template)
    if (any(directives.count(token) != 1 for token in ("%Y", "%m", "%d"))
            or any(token not in ("%Y", "%m", "%d", "%H", "%M", "%S") for token in directives)
            or len(set(directives)) != len(directives)
            or re.search(r"[^\-/:. \t]", re.sub(r"%.", "", template))):
        return None
    pattern = re.escape(template)
    for token in directives:
        pattern = pattern.replace(re.escape(token), r"\d{4}" if token == "%Y" else r"\d{2}")
    # PostgreSQL stops reading when its template ends.  Retain the common,
    # deterministic date-only template applied to an ISO timestamp as well.
    suffix = r"(?:[ T]\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?)?" if not any(
        token in directives for token in ("%H", "%M", "%S")) else ""
    match = re.fullmatch("(" + pattern + ")" + suffix + r"\s*", text)
    if match is None:
        return None
    try:
        return datetime.datetime.strptime(match[1], template).date()
    except ValueError:
        return None


def _unit(node):
    if isinstance(node, (exp.Var, exp.Literal, exp.Identifier)):
        return str(node.this).upper().rstrip("S")
    return None


def _interval(amount, unit):
    if unit is None and isinstance(amount, str):
        match = _INTERVAL.fullmatch(amount)
        if match is None:
            return None
        amount, unit = match[1], match[2].upper().rstrip("S")
    try:
        amount = Decimal(str(amount))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not amount.is_finite():
        return None
    if unit in ("YEAR", "MONTH"):
        months = amount * (12 if unit == "YEAR" else 1)
        return Interval(int(months), 0, 0) if months == int(months) else None
    if unit in ("WEEK", "DAY"):
        days = amount * (7 if unit == "WEEK" else 1)
        whole_days = int(days)
        micros = (days - whole_days) * 86400000000
        return Interval(0, whole_days, int(micros)) if micros == int(micros) else None
    factors = {"HOUR": 3600000000, "MINUTE": 60000000, "SECOND": 1000000,
               "MILLISECOND": 1000, "MICROSECOND": 1}
    if unit in factors:
        micros = amount * factors[unit]
        return Interval(0, 0, int(micros)) if micros == int(micros) else None
    return None


def _cast(node):
    target = node.args.get("to")
    # Length/precision, TRY_CAST and formatted CAST have additional semantics.
    if (not isinstance(target, exp.DataType) or target.expressions
            or node.args.get("format") or node.args.get("safe")
            or node.args.get("action") or node.args.get("default")):
        return None
    kind = target.this
    item = value(node.this)
    if kind == exp.DataType.Type.DATE:
        return _as_date(item)
    if kind == exp.DataType.Type.TIMESTAMP:
        if isinstance(item, datetime.datetime):
            return item if item.tzinfo is None else None
        if isinstance(item, datetime.date):
            return datetime.datetime.combine(item, datetime.time())
        return _timestamp(item)
    if kind in (exp.DataType.Type.TEXT, exp.DataType.Type.VARCHAR):
        # date/timestamp -> text depends on DateStyle. Do not stringify them.
        if isinstance(item, (str, int, Decimal)):
            return render_value(item)
        return None
    integer_limits = {exp.DataType.Type.SMALLINT: 2 ** 15,
                      exp.DataType.Type.INT: 2 ** 31,
                      exp.DataType.Type.BIGINT: 2 ** 63}
    if kind in integer_limits:
        if isinstance(item, int):
            result = item
        elif isinstance(item, str) and re.fullmatch(r"\s*[+-]?\d+\s*", item):
            result = int(item)
        else:
            return None
        limit = integer_limits[kind]
        return result if -limit <= result < limit else None
    if kind == exp.DataType.Type.DECIMAL and isinstance(item, (str, int, Decimal)):
        text = str(item).strip()
        if _NUMBER.fullmatch(text):
            result = Decimal(text)
            return result if result.is_finite() else None
    if kind == exp.DataType.Type.INTERVAL and isinstance(item, str):
        return _interval(item, None)
    return None


def _truncate(node):
    if node.args.get("zone") is not None:
        return None
    item = value(node.this)
    # pg_cast permits date -> timestamp and date -> timestamptz. pg_type marks
    # timestamptz preferred in their category, so the date_trunc(date) call
    # selects its timestamptz overload (typeconv-func's best-match rule 4.4).
    # That overload needs TimeZone. An explicit TIMESTAMP cast is deterministic.
    if not isinstance(item, datetime.datetime) or item.tzinfo is not None:
        return None
    unit = _unit(node.args.get("unit"))
    if unit == "YEAR":
        return item.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    if unit == "QUARTER":
        return item.replace(month=(item.month - 1) // 3 * 3 + 1, day=1,
                            hour=0, minute=0, second=0, microsecond=0)
    if unit == "MONTH":
        return item.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if unit == "WEEK":
        return (item - datetime.timedelta(days=item.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0)
    if unit == "DAY":
        return item.replace(hour=0, minute=0, second=0, microsecond=0)
    if unit == "HOUR":
        return item.replace(minute=0, second=0, microsecond=0)
    if unit == "MINUTE":
        return item.replace(second=0, microsecond=0)
    if unit == "SECOND":
        return item.replace(microsecond=0)
    return None


def _shift(item, interval):
    if not isinstance(item, datetime.datetime):
        item = datetime.datetime.combine(item, datetime.time())
    months = item.year * 12 + item.month - 1 + interval.months
    year, month = divmod(months, 12)
    month += 1
    item = item.replace(year=year, month=month,
                        day=min(item.day, calendar.monthrange(year, month)[1]))
    return item + datetime.timedelta(days=interval.days, microseconds=interval.microseconds)


def _arithmetic(node):
    left, right = value(node.this), value(node.expression)
    sign = -1 if isinstance(node, exp.Sub) else 1
    if isinstance(left, datetime.datetime) and isinstance(node.expression, exp.Literal) and node.expression.is_string:
        right = _interval(right, None)
    if isinstance(left, Interval) and isinstance(right, Interval):
        return Interval(*(a + sign * b for a, b in zip(left, right)))
    if sign == 1 and isinstance(left, Interval) and isinstance(right, datetime.date):
        left, right = right, left
    if isinstance(left, datetime.date):
        if isinstance(right, Interval):
            return _shift(left, Interval(*(sign * part for part in right)))
        if isinstance(left, datetime.datetime):
            return None
        if isinstance(right, int):
            return left + datetime.timedelta(days=sign * right)
        if sign == -1 and isinstance(right, datetime.date) and not isinstance(right, datetime.datetime):
            return (left - right).days
    if sign == 1 and isinstance(left, int) and isinstance(right, datetime.date) and not isinstance(right, datetime.datetime):
        return right + datetime.timedelta(days=left)
    if isinstance(left, int) and isinstance(right, int):
        result = left + sign * right
        # PostgreSQL's integer arithmetic can overflow rather than widen.
        limit = 2 ** 31 if all(-2 ** 31 <= part < 2 ** 31 for part in (left, right)) else 2 ** 63
        if all(-2 ** 63 <= part < 2 ** 63 for part in (left, right)):
            return result if -limit <= result < limit else None
        return result
    return None


def value(node):
    """Return a reliable scalar value, or None when evaluation is not known.

    Possible values are str, int, Decimal, date, naive datetime and Interval.
    ``None`` represents unknown evaluation, including SQL NULL.  No column type,
    DateStyle or TimeZone is inferred, and unsupported expressions are retained.
    """
    if node is None:
        return None
    try:
        if isinstance(node, exp.Paren):
            return value(node.this)
        if isinstance(node, exp.Literal):
            if node.is_string:
                return node.this
            text = str(node.this)
            if re.fullmatch(r"[+-]?\d+", text):
                return int(text)
            if _NUMBER.fullmatch(text):
                result = Decimal(text)
                return result if result.is_finite() else None
            return None
        if isinstance(node, exp.Cast):
            return _cast(node)
        if isinstance(node, exp.StrToDate):
            return _format_date(value(node.this), value(node.args.get("format")))
        # TO_TIMESTAMP returns timestamptz and needs the session's TimeZone.
        if isinstance(node, exp.StrToTime):
            return None
        if isinstance(node, exp.Date):
            return _as_date(value(node.this)) if not node.expressions and node.args.get("zone") is None else None
        if isinstance(node, (exp.DateTrunc, exp.TimestampTrunc)):
            return _truncate(node)
        if isinstance(node, exp.Interval):
            return _interval(value(node.this), _unit(node.args.get("unit")))
        if isinstance(node, exp.Neg):
            item = value(node.this)
            if isinstance(item, Interval):
                return Interval(*(-part for part in item))
            if isinstance(item, Decimal):
                return item.copy_negate()
            return -item if isinstance(item, int) else None
        if isinstance(node, (exp.Add, exp.Sub)):
            return _arithmetic(node)
    except (ValueError, TypeError, OverflowError, InvalidOperation):
        # Invalid or out-of-range constants are evidence, never invented values.
        return None
    return None


def render_value(item):
    """Render an extracted value for evidence, without erasing timestamp time."""
    if isinstance(item, datetime.datetime):
        return item.isoformat(sep=" ")
    if isinstance(item, datetime.date):
        return item.isoformat()
    if isinstance(item, Decimal):
        return format(item, "f")
    if isinstance(item, Interval):
        return "{} months {} days {} microseconds".format(*item)
    return str(item)


def _temporal_literal(item):
    kind = "TIMESTAMP" if isinstance(item, datetime.datetime) else "DATE"
    return exp.Cast(this=exp.Literal.string(render_value(item)), to=exp.DataType.build(kind))


def normalize_expression(node):
    """Copy a value/predicate AST and fold complete, reliable date expressions.

    Scalar expressions that cannot be evaluated are barriers: folding their
    children could change an overload, a CAST or a formatting function.  Only
    predicate structure is traversed.  An explicit DATE common type in an IN
    list permits deterministic date input strings to become typed DATE values.
    """
    if node is None:
        return None
    result = node.copy()

    def normalize(current):
        item = value(current)
        if isinstance(item, datetime.date):
            return _temporal_literal(item)
        if isinstance(current, exp.In):
            expressions = current.expressions
            date_context = any(type(value(member)) is datetime.date for member in expressions)
            date_context = date_context and all(
                isinstance(member, exp.Null) or type(value(member)) is datetime.date
                or (isinstance(member, exp.Literal) and member.is_string)
                for member in expressions)
            members = []
            for member in expressions:
                parsed = _as_date(value(member)) if date_context else None
                members.append(_temporal_literal(parsed) if parsed is not None else normalize(member))
            current.set("expressions", members)
            current.set("this", normalize(current.this))
        elif isinstance(current, exp.Between):
            for key in ("this", "low", "high"):
                child = current.args.get(key)
                if child is not None:
                    current.set(key, normalize(child))
        elif isinstance(current, (exp.And, exp.Or, exp.EQ, exp.NEQ, exp.GT,
                                  exp.GTE, exp.LT, exp.LTE, exp.Is, exp.Like, exp.ILike)):
            current.set("this", normalize(current.this))
            current.set("expression", normalize(current.expression))
        elif isinstance(current, (exp.Paren, exp.Not, exp.Where, exp.Having)):
            current.set("this", normalize(current.this))
        return current

    return normalize(result)
