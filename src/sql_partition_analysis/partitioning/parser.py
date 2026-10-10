"""Token-based PostgreSQL syntax compatibility for the Python 3.7 parser pin.

SQLGlot 26.9 loses signs in INTERVAL '-1 DAY' and rejects DATE 'x'::DATE.
Rewrite equivalent token forms before parsing; never edit strings or comments
by searching the SQL text with a global regular expression.
"""
import re

import sqlglot
from sqlglot import exp
from sqlglot.dialects.postgres import Postgres
from sqlglot.tokens import TokenType

from .preprocessing import (
    prepare_sql, recover_identifier_quote_encoding, sql_start_keyword, is_sql_statement,
)


_INTERVAL = re.compile(
    r'([+-]?\d+(?:\.\d+)?)\s+(YEAR|MONTH|WEEK|DAY|HOUR|MINUTE|SECOND|MILLISECOND|MICROSECOND)S?',
    re.IGNORECASE,
)


def _preserve_function_arguments(name, builder, supported_count, template_index=None):
    """Guard lossy dialect builders without losing their containing statement.

    A function with additional arguments may be a valid dialect overload. Keep
    every argument in an opaque function rather than silently lower its arity.
    PostgreSQL format templates containing '%' must also retain their original
    spelling: SQLGlot's strptime conversion otherwise conflates literal '%Y'
    with the genuine PostgreSQL year pattern 'YYYY'.
    Ordinary calls retain SQLGlot's existing typed AST and constant handling.
    """
    def build(arguments):
        if len(arguments) > supported_count:
            return exp.Anonymous(this=name, expressions=arguments)
        if template_index is not None and len(arguments) > template_index:
            template = arguments[template_index]
            if isinstance(template, exp.Expression) and any(
                    literal.is_string and "%" in literal.this
                    for literal in template.find_all(exp.Literal)):
                return exp.Anonymous(this=name, expressions=arguments)
        return builder(arguments)
    return build


class CompatiblePostgres(Postgres):
    """Accept Greenplum's unreserved VALUES in identifier alias contexts.

    Columns are already handled by PostgreSQL's VALUES_FOLLOWED_BY_PAREN rule.
    Do not add VALUES to generic ID_VAR_TOKENS: the statement/expression parser
    must still recognize VALUES row constructors and INSERT ... VALUES.
    """
    class Parser(Postgres.Parser):
        ALIAS_TOKENS = Postgres.Parser.ALIAS_TOKENS | {TokenType.VALUES}
        TABLE_ALIAS_TOKENS = Postgres.Parser.TABLE_ALIAS_TOKENS | {TokenType.VALUES}
        FUNCTIONS = dict(Postgres.Parser.FUNCTIONS)
        # The pinned builders read only arguments[0:2], while their AST classes
        # have optional zone/safe fields that let a third input pass validation.
        for _function_name in ("DATE_TRUNC", "TO_DATE", "TO_TIMESTAMP"):
            FUNCTIONS[_function_name] = _preserve_function_arguments(
                _function_name, Postgres.Parser.FUNCTIONS[_function_name], 2,
                template_index=1 if _function_name in ("TO_DATE", "TO_TIMESTAMP") else None,
            )
        del _function_name

        def _parse_alias(self, this, explicit=False):
            # _parse_statement first tries an expression before trying SELECT/
            # VALUES. A keyword alias cannot exist without a left expression.
            if this is None and self._curr and self._curr.token_type == TokenType.VALUES:
                return None
            return super()._parse_alias(this, explicit=explicit)


def compatible_postgres(sql):
    sql, _ = prepare_sql(sql)
    tokens = sqlglot.tokenize(sql, read='postgres')
    edits = []
    for index, token in enumerate(tokens[:-1]):
        literal = tokens[index + 1]
        if literal.token_type != TokenType.STRING:
            continue
        if token.token_type == TokenType.INTERVAL:
            match = _INTERVAL.fullmatch(literal.text.strip())
            if match:
                edits.append((literal.start, literal.end + 1,
                              "'{}' {}".format(match[1], match[2].upper())))
        elif token.token_type == TokenType.DATE and index + 2 < len(tokens):
            if tokens[index + 2].token_type == TokenType.DCOLON:
                edits.append((token.start, literal.end + 1,
                              '(' + sql[token.start:literal.end + 1] + ')'))
    for start, end, replacement in reversed(edits):
        sql = sql[:start] + replacement + sql[end:]
    return sql


def parse_postgres(sql):
    return sqlglot.parse(compatible_postgres(sql), read=CompatiblePostgres)


def parse_one_postgres(sql):
    return sqlglot.parse_one(compatible_postgres(sql), read=CompatiblePostgres)
