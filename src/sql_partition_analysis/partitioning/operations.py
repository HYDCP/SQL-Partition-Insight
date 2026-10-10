"""Conservative helpers for partition DDL and positional write assignments.

These helpers identify values mentioned by SQL, not physical partitions or
successful row writes. Unsupported syntax keeps its text instead of guessing.
"""

import datetime
import re

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
from sqlglot.tokens import TokenType
from .lexical import tokenize_postgres
from .parser import parse_one_postgres, recover_identifier_quote_encoding


def _tokens(text):
    return tokenize_postgres(text)


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


def recover_copy_header(text):
    """Keep a complete COPY relation/direction even if its options fail parsing.

    Returns (Table, direction, incomplete_header).  FROM writes, TO reads.
    Direction is taken only immediately after the relation's optional balanced
    column list. Words inside literals or column identifiers are never evidence.
    """
    try:
        tokens = _tokens(text)
    except Exception:
        # Tokenize only the complete lexical prefix. A broken option string
        # after FROM/TO must not erase an already complete COPY header.
        try:
            tokens = _tokens(_complete_lexical_prefix(text))
        except Exception:
            return None
    if len(tokens) < 2 or not _word(tokens[0], 'COPY') or tokens[1].token_type == TokenType.L_PAREN:
        return None
    index = 2
    while index + 1 < len(tokens) and tokens[index].token_type == TokenType.DOT:
        index += 2
    if index < len(tokens) and tokens[index].token_type == TokenType.DOT:
        # A qualified target may have more components after the final dot.
        # Its shorter prefix is not evidence of a complete relation.
        return None
    name = text[tokens[1].start:tokens[index - 1].end + 1]
    try:
        table = normalize_identifiers(sqlglot.parse_one('SELECT * FROM ' + name, read='postgres'), dialect='postgres').find(exp.Table)
        if table is None or not isinstance(table.this, exp.Identifier):
            return None
        if not all(part.name for part in table.parts):
            # An encoded relation begins with an empty quoted token in the
            # normal lexer. Recover only its complete head, never a truncated
            # field list or data body. The caller must expose this assumption.
            boundary = _copy_relation_end(tokens, text)
            head = recover_identifier_quote_encoding(text[:boundary]) if boundary is not None else None
            if head is None:
                return None
            recovered = recover_copy_header(head + text[boundary:])
            if recovered is not None:
                recovered[0].meta['identifier_quote_recovery'] = True
            return recovered
    except Exception:
        return None
    if index < len(tokens) and tokens[index].token_type == TokenType.L_PAREN:
        group = _parenthesized(text, tokens, index)
        if group is None:
            return table, None, True
        index = group[1]
    if index >= len(tokens) or tokens[index].token_type == TokenType.SEMICOLON:
        return table, None, True
    if _word(tokens[index], 'FROM'):
        return table, 'COPY写入', False
    if _word(tokens[index], 'TO'):
        return table, 'COPY读取', False
    return table, None, False


def _copy_relation_end(tokens, text):
    """Locate a complete relation without reading words inside encoded names."""
    index, end = 1, None
    while index < len(tokens):
        token = tokens[index]
        if token.token_type == TokenType.IDENTIFIER and not token.text:
            frame = re.match(r'""[^"\x00]+""(?!")', text[token.start:])
            if frame is None:
                return None
            end = token.start + frame.end()
            if end < len(text) and (text[end].isalnum() or text[end] in '_$"'):
                return None
            fragment = text[token.start:end]
            if recover_identifier_quote_encoding(fragment) is None:
                return None
            while index < len(tokens) and tokens[index].end < end:
                index += 1
        elif (token.token_type == TokenType.IDENTIFIER
              or token.token_type not in (TokenType.STRING, TokenType.HEREDOC_STRING)
              and re.fullmatch(r'[^\W\d][\w$]*', token.text, re.UNICODE)):
            end, index = token.end + 1, index + 1
        else:
            return None
        if index >= len(tokens) or tokens[index].token_type != TokenType.DOT:
            return end
        index += 1
    return None


def _complete_lexical_prefix(text):
    """Stop before an unfinished quote/comment, preserving complete tokens.

    This recovery does not repair SQL. Its sole purpose is to let the COPY
    header parser inspect evidence preceding a malformed trailing token.
    """
    index, size = 0, len(text)
    while index < size:
        start = index
        if text.startswith('--', index):
            end = text.find('\n', index + 2)
            index = size if end < 0 else end + 1
        elif text.startswith('/*', index):
            depth, index = 1, index + 2
            while index < size and depth:
                if text.startswith('/*', index):
                    depth, index = depth + 1, index + 2
                elif text.startswith('*/', index):
                    depth, index = depth - 1, index + 2
                else:
                    index += 1
            if depth:
                return text[:start]
        elif text[index] in ('\"', "'"):
            quote = text[index]
            escaped = quote == "'" and index > 0 and text[index - 1] in 'eE' and (
                index == 1 or not (text[index - 2].isalnum() or text[index - 2] in '_$'))
            index += 1
            while index < size:
                if escaped and text[index] == '\\':
                    index += 2
                elif text[index] == quote:
                    if index + 1 < size and text[index + 1] == quote:
                        index += 2
                    else:
                        index += 1
                        break
                else:
                    index += 1
            else:
                return text[:start - 1 if escaped else start]
        elif text[index] == '$' and (index == 0 or not (text[index - 1].isalnum() or text[index - 1] in '_$')):
            match = re.match(r'\$(?:[A-Za-z_][A-Za-z_0-9]*)?\$', text[index:])
            if match:
                marker = match.group()
                end = text.find(marker, index + len(marker))
                if end < 0:
                    return text[:start]
                index = end + len(marker)
            else:
                index += 1
        else:
            index += 1
    return text


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


def _single_row_update_values(query):
    """Unwrap an UPDATE row subquery only when its single row is proved.

    A FROM-free, clause-free SELECT and one VALUES tuple have fixed arity and
    cardinality. Require reliably evaluable scalar projections as well:
    unknown functions may return a set, and nested queries may return zero or
    several rows. Other query forms retain the existing unknown assignment.
    """
    from .constants import value as constant_value

    while isinstance(query, (exp.Subquery, exp.Paren)):
        if any(item for key, item in query.args.items() if key != "this"):
            return None
        query = query.this
    if isinstance(query, exp.Select):
        if any(item for key, item in query.args.items() if key != "expressions"):
            return None
        values = query.expressions
    elif isinstance(query, exp.Values):
        if (any(item for key, item in query.args.items() if key != "expressions")
                or len(query.expressions) != 1
                or not isinstance(query.expressions[0], exp.Tuple)):
            return None
        values = query.expressions[0].expressions
    else:
        return None
    values = [item.this if isinstance(item, exp.Alias) else item for item in values]
    if not values or any(constant_value(item) is None for item in values):
        return None
    return values


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
            elif isinstance(assignment.this, exp.Tuple):
                targets = assignment.this.expressions
                values = (assignment.expression.expressions
                          if isinstance(assignment.expression, exp.Tuple)
                          else _single_row_update_values(assignment.expression))
                if (values is not None and len(targets) == len(values)
                        and all(isinstance(x, exp.Column) for x in targets)):
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
