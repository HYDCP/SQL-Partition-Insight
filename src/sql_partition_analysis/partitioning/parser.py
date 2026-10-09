"""Token-based PostgreSQL syntax compatibility for the Python 3.7 parser pin.

SQLGlot 26.9 loses signs in INTERVAL '-1 DAY' and rejects DATE 'x'::DATE.
Rewrite equivalent token forms before parsing; never edit strings or comments
by searching the SQL text with a global regular expression.
"""
import re

import sqlglot
from sqlglot.tokens import TokenType


_INTERVAL = re.compile(
    r'([+-]?\d+(?:\.\d+)?)\s+(YEAR|MONTH|WEEK|DAY|HOUR|MINUTE|SECOND|MILLISECOND|MICROSECOND)S?',
    re.IGNORECASE,
)


def compatible_postgres(sql):
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
    return sqlglot.parse(compatible_postgres(sql), read='postgres')


def parse_one_postgres(sql):
    return sqlglot.parse_one(compatible_postgres(sql), read='postgres')
