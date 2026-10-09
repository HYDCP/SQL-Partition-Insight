"""Conservative helpers for partition DDL and positional write assignments.

These helpers identify values mentioned by SQL, not physical partitions or
successful row writes. Unsupported syntax keeps its text instead of guessing.
"""

import datetime

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
from sqlglot.tokens import TokenType
from .parser import parse_one_postgres


def _tokens(text):
    return sqlglot.tokenize(text, read="postgres")


def _word(token, word):
    return token.token_type not in (
        TokenType.IDENTIFIER, TokenType.STRING, TokenType.HEREDOC_STRING
    ) and token.text.upper() == word


def split_sql_statements(text):
    """Split on SQL semicolons, preserving quoted strings and comments.

    Invalid tokenization returns the original statement so the caller's normal
    parse-failure handling can report it without dropping the log record.
    """
    try:
        tokens = _tokens(text)
    except Exception:
        return [text] if text.strip() else []
    result, start = [], 0
    for token in tokens:
        if token.token_type == TokenType.SEMICOLON:
            part = text[start:token.start].strip()
            if part and _tokens(part):
                result.append(part)
            start = token.end + 1
    tail = text[start:].strip()
    if tail and _tokens(tail):
        result.append(tail)
    return result


def parse_alter(text):
    """Return ``(normalized Table, action_text)`` for one ALTER TABLE, else None.

    ONLY and IF EXISTS are modifiers, never object names. The caller should
    split a multi-statement log with ``split_sql_statements`` first.
    """
    try:
        tokens = _tokens(text)
        if tokens and tokens[-1].token_type == TokenType.SEMICOLON:
            tokens = tokens[:-1]
        if any(t.token_type == TokenType.SEMICOLON for t in tokens):
            return None
        if len(tokens) < 4 or not _word(tokens[0], "ALTER") or not _word(tokens[1], "TABLE"):
            return None
        index = 2
        if _word(tokens[index], "IF"):
            if index + 1 >= len(tokens) or not _word(tokens[index + 1], "EXISTS"):
                return None
            index += 2
        if index < len(tokens) and _word(tokens[index], "ONLY"):
            index += 1
        if index >= len(tokens):
            return None
        first = index
        index += 1
        while index < len(tokens) and tokens[index].token_type == TokenType.DOT:
            index += 2
        if index >= len(tokens):
            return None
        name = text[tokens[first].start:tokens[index - 1].end + 1]
        if tokens[index].token_type == TokenType.STAR:
            index += 1
        if index >= len(tokens):
            return None
        action_words = ("ADD", "DROP", "ALTER", "RENAME", "ATTACH", "DETACH", "SET", "RESET",
                        "ENABLE", "DISABLE", "OWNER", "INHERIT", "NO", "VALIDATE", "OF",
                        "NOT", "SPLIT", "EXCHANGE", "TRUNCATE", "EXPAND", "REORGANIZE")
        if not any(_word(tokens[index], word) for word in action_words):
            return None
        tree = normalize_identifiers(
            sqlglot.parse_one("SELECT * FROM " + name, read="postgres"),
            dialect="postgres",
        )
        table = tree.find(exp.Table)
        if table is None or not isinstance(table.this, exp.Identifier):
            return None
        action = text[tokens[index].start:tokens[-1].end + 1].strip()
        return table, action
    except Exception:
        return None


def _parenthesized(text, tokens, index):
    """Return a balanced parenthesized body and the next token index."""
    if index >= len(tokens) or tokens[index].token_type != TokenType.L_PAREN:
        return None
    first, depth = index, 0
    while index < len(tokens):
        kind = tokens[index].token_type
        if kind == TokenType.L_PAREN:
            depth += 1
        elif kind == TokenType.R_PAREN:
            depth -= 1
            if depth == 0:
                return text[tokens[first].end + 1:tokens[index].start], index + 1
        index += 1
    return None


def _literal_value(node):
    """Format plain scalar/date literals only; do not evaluate expressions."""
    if isinstance(node, exp.Paren):
        return _literal_value(node.this)
    if isinstance(node, exp.Cast) and node.args.get("to").is_type(exp.DataType.Type.DATE):
        if isinstance(node.this, exp.Literal) and node.this.is_string:
            for pattern in ("%Y-%m-%d", "%Y%m%d"):
                try:
                    return datetime.datetime.strptime(node.this.this, pattern).date().isoformat()
                except ValueError:
                    pass
        return None
    if isinstance(node, exp.Literal):
        if node.is_string:
            for pattern in ("%Y-%m-%d", "%Y%m%d"):
                try:
                    return datetime.datetime.strptime(node.this, pattern).date().isoformat()
                except ValueError:
                    pass
        return node.sql(dialect="postgres", comments=False) if node.is_string else node.this
    return None


def _values(body, render_value):
    """Render a value list through ASTs; unknown expressions retain their SQL."""
    tree = parse_one_postgres("SELECT " + body)
    if not isinstance(tree, exp.Select) or not tree.expressions:
        return body.strip()
    result = []
    for value in tree.expressions:
        rendered = render_value(value) if render_value else _literal_value(value)
        if rendered is None:
            rendered = value.sql(dialect="postgres", comments=False)
        result.append(str(rendered))
    return ",".join(result)


def describe_partition_action(action_text, columns, render_value=None):
    """Describe explicit partition targets, or return None for ordinary ALTER.

    ``render_value`` is an optional ``Expression -> scalar string | None``
    callback. It can reuse the analyzer's safe constant evaluation. The default
    evaluates literal dates only. Unknown expressions remain expressions.
    Existing single-key ADD START/END and TRUNCATE FOR labels stay compatible.
    """
    try:
        tokens = _tokens(action_text)
        if tokens and tokens[-1].token_type == TokenType.SEMICOLON:
            tokens = tokens[:-1]
        if len(tokens) < 2:
            return None
        verbs = ("ADD", "DROP", "TRUNCATE", "EXCHANGE", "RENAME", "SPLIT", "ALTER", "ATTACH", "DETACH")
        if not any(_word(tokens[0], verb) for verb in verbs):
            return None
        index = 1
        if _word(tokens[index], "DEFAULT"):
            index += 1
        if index >= len(tokens) or not _word(tokens[index], "PARTITION"):
            return None
        index += 1
        # An action list has multiple independent targets; preserve all actions.
        depth = 0
        for token in tokens[index:]:
            if token.token_type == TokenType.L_PAREN:
                depth += 1
            elif token.token_type == TokenType.R_PAREN:
                depth -= 1
            elif token.token_type in (TokenType.COMMA, TokenType.SEMICOLON) and depth == 0:
                return action_text.strip()
        if index < len(tokens) and _word(tokens[index], "IF"):
            index += 1
            if index < len(tokens) and _word(tokens[index], "NOT"):
                index += 1
            if index >= len(tokens) or not _word(tokens[index], "EXISTS"):
                return action_text.strip()
            index += 1
        if index >= len(tokens):
            return action_text.strip()
        if _word(tokens[index], "FOR"):
            group = _parenthesized(action_text, tokens, index + 1)
            if group is None or group[1] != len(tokens):
                return action_text.strip()
            return "FOR(" + _values(group[0], render_value) + ")"
        if any(_word(tokens[index], word) for word in ("START", "END", "VALUES", "WITH")):
            return action_text.strip()
        identifier = action_text[tokens[index].start:tokens[index].end + 1]
        table = normalize_identifiers(sqlglot.parse_one("SELECT * FROM " + identifier, read="postgres"), dialect="postgres").find(exp.Table)
        if table is None:
            return action_text.strip()
        name = "分区名=" + table.name
        index += 1
        if index == len(tokens):
            return name
        if _word(tokens[index], "VALUES"):
            group = _parenthesized(action_text, tokens, index + 1)
            if group is None:
                return action_text.strip()
            return name + "；VALUES(" + _values(group[0], render_value) + ")"
        if not _word(tokens[index], "START"):
            return action_text.strip()
        start = _parenthesized(action_text, tokens, index + 1)
        if start is None:
            return action_text.strip()
        index = start[1]
        left = "["
        if index < len(tokens) and any(_word(tokens[index], word) for word in ("INCLUSIVE", "EXCLUSIVE")):
            left = "(" if _word(tokens[index], "EXCLUSIVE") else "["
            index += 1
        if index >= len(tokens) or not _word(tokens[index], "END"):
            return action_text.strip()
        end = _parenthesized(action_text, tokens, index + 1)
        if end is None:
            return action_text.strip()
        index = end[1]
        right = ")"
        if index < len(tokens) and any(_word(tokens[index], word) for word in ("INCLUSIVE", "EXCLUSIVE")):
            right = "]" if _word(tokens[index], "INCLUSIVE") else ")"
        return name + "；范围" + left + _values(start[0], render_value) + "," + _values(end[0], render_value) + right
    except Exception:
        # The shape established a partition action; preserve evidence if its
        # values or identifiers cannot be parsed safely.
        return action_text.strip()


def _has_projection_star(value):
    value = value.this if isinstance(value, exp.Alias) else value
    return isinstance(value, exp.Star) or isinstance(value, exp.Column) and isinstance(value.this, exp.Star)


def _write_rows(query):
    if isinstance(query, (exp.Subquery, exp.Paren)):
        return _write_rows(query.this)
    if isinstance(query, exp.Union):
        left, right = _write_rows(query.this), _write_rows(query.expression)
        return left + right if left is not None and right is not None else None
    if isinstance(query, exp.Select):
        return None if any(_has_projection_star(x) for x in query.expressions) else [query.expressions]
    if isinstance(query, exp.Values):
        return [row.expressions for row in query.expressions if isinstance(row, exp.Tuple)]
    return None


def write_assignments(tree):
    """Return ``[(field, expression, branch)]`` for safely mapped writes.

    UNION leaves and VALUES rows have independent branch numbers. No target
    position is inferred across SELECT * or mismatched projection lengths; an
    unsupported branch makes the whole INSERT/CTAS assignment set unknown.
    UPDATE assignments are independent of old-row WHERE predicates.
    """
    tree = normalize_identifiers(tree.copy(), dialect="postgres")
    if isinstance(tree, exp.Update):
        result = []
        for assignment in tree.expressions:
            if not isinstance(assignment, exp.EQ):
                continue
            if isinstance(assignment.this, exp.Column):
                result.append((assignment.this.name, assignment.expression, 0))
            elif isinstance(assignment.this, exp.Tuple) and isinstance(assignment.expression, exp.Tuple):
                targets, values = assignment.this.expressions, assignment.expression.expressions
                if len(targets) == len(values) and all(isinstance(x, exp.Column) for x in targets):
                    result.extend((column.name, value, 0) for column, value in zip(targets, values))
        return result
    if not isinstance(tree, (exp.Insert, exp.Create)):
        return []
    if isinstance(tree, exp.Create) and tree.args.get("kind") != "TABLE":
        return []
    source = tree.args.get("expression")
    rows = _write_rows(source)
    if not rows:
        return []
    if isinstance(tree.this, exp.Schema):
        columns = [x.name for x in tree.this.expressions]
    elif isinstance(tree, exp.Create):
        # CTAS output names are defined by the first projection, not a catalog
        # column order. Require explicit aliases/direct column names throughout.
        columns = [x.alias_or_name if isinstance(x, (exp.Alias, exp.Column)) else "" for x in rows[0]]
    else:
        return []
    if not columns or any(not c for c in columns) or any(len(row) != len(columns) for row in rows):
        return []
    return [(column, value.this if isinstance(value, exp.Alias) else value, branch)
            for branch, row in enumerate(rows) for column, value in zip(columns, row)]
