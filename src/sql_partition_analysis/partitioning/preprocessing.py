"""Conservative, token-boundary compatibility for logged PostgreSQL/Greenplum.

The input remains the evidence. ``prepare_sql`` returns a separate parse view
and explanations, never an inferred executable statement. Tokenization failure
leaves the statement intact so normal diagnostics can report the failure.
"""
import re

import sqlglot
from sqlglot import exp
from sqlglot.tokens import TokenType


_QUOTED = {TokenType.IDENTIFIER, TokenType.STRING, TokenType.HEREDOC_STRING}
_SQL_STARTS = set((
    "SELECT WITH CREATE INSERT UPDATE DELETE COPY ALTER DROP TRUNCATE ANALYZE "
    "VACUUM COMMENT SET SHOW BEGIN COMMIT ROLLBACK START END EXPLAIN VALUES"
).split())
_PLAIN_IDENTIFIER = re.compile(r"[^\W\d][\w$]*\Z", re.UNICODE)
_DOLLAR_DELIMITER = re.compile(r"\$(?:[^\W\d]\w*)?\$", re.UNICODE)


def _word(token, word):
    return token.token_type not in _QUOTED and token.text.upper() == word


def _skip_comment(text, index):
    """Skip one line or nested block comment; None means unterminated block."""
    if text.startswith("--", index):
        end = text.find("\n", index + 2)
        return len(text) if end < 0 else end + 1
    if text.startswith("/*", index):
        depth, cursor = 1, index + 2
        while cursor < len(text):
            if text.startswith("/*", cursor):
                depth += 1
                cursor += 2
            elif text.startswith("*/", cursor):
                depth -= 1
                cursor += 2
                if not depth:
                    return cursor
            else:
                cursor += 1
        return None
    return index


def sql_start_keyword(text):
    """Return the first unquoted SQL keyword, or None (including log wrappers).

    Only leading trivia is scanned. Tokenizing the isolated initial word also
    works for truncated COPY/SELECT records whose later quote is unterminated.
    Strings, quoted identifiers and comments cannot impersonate SQL keywords.
    """
    text = text.replace("\x18", "\n")
    cursor = 0
    while cursor < len(text):
        if text[cursor].isspace():
            cursor += 1
            continue
        next_cursor = _skip_comment(text, cursor)
        if next_cursor is None:
            return None
        if next_cursor != cursor:
            cursor = next_cursor
            continue
        break
    if cursor >= len(text) or not (text[cursor].isalpha() or text[cursor] == "_"):
        return None
    end = cursor
    while end < len(text) and (text[end].isalnum() or text[end] in "_$"):
        end += 1
    if end == cursor:
        return None
    try:
        tokens = sqlglot.tokenize(text[cursor:end], read="postgres")
    except Exception:
        return None
    if len(tokens) == 1 and tokens[0].token_type not in _QUOTED:
        keyword = tokens[0].text.upper()
        return keyword if keyword in _SQL_STARTS else None
    return None


def is_sql_statement(text):
    return sql_start_keyword(text) is not None


def _create_table(tokens):
    if not tokens or not _word(tokens[0], "CREATE"):
        return False
    index = 1
    # CREATE OR REPLACE is accepted by some Greenplum versions/extensions.
    if index + 1 < len(tokens) and _word(tokens[index], "OR") and _word(tokens[index + 1], "REPLACE"):
        index += 2
    while index < len(tokens) and any(_word(tokens[index], word) for word in (
            "TEMP", "TEMPORARY", "UNLOGGED", "GLOBAL", "LOCAL")):
        index += 1
    return index + 1 < len(tokens) and _word(tokens[index], "TABLE")


def _identifier_token(token):
    return (token.token_type == TokenType.IDENTIFIER
            or token.token_type not in _QUOTED and bool(_PLAIN_IDENTIFIER.fullmatch(token.text)))


def _distribution_columns(tokens):
    # Greenplum optionally accepts an operator class after each column name.
    # Its name may have a schema qualifier, but expressions are not columns.
    groups, group = [], []
    for token in tokens:
        if token.token_type == TokenType.COMMA:
            groups.append(group)
            group = []
        else:
            group.append(token)
    groups.append(group)
    for group in groups:
        if not group or not _identifier_token(group[0]):
            return False
        if len(group) == 1:
            continue
        if len(group) == 2 and _identifier_token(group[1]):
            continue
        if (len(group) == 4 and _identifier_token(group[1])
                and group[2].token_type == TokenType.DOT and _identifier_token(group[3])):
            continue
        return False
    return True


def _complete_table_creation(tree):
    """Reject SQLGlot's permissive empty SELECT/CREATE fallback ASTs."""
    if not isinstance(tree, exp.Create) or tree.args.get("kind") != "TABLE":
        return False
    target = tree.this
    table = target.this if isinstance(target, exp.Schema) else target
    if not isinstance(table, exp.Table) or not isinstance(table.this, exp.Identifier) or not table.name:
        return False
    query = tree.args.get("expression")
    if query is not None:
        if not isinstance(query, (exp.Query, exp.Values)):
            return False
        if any(not select.expressions for select in query.find_all(exp.Select)):
            return False
    elif not isinstance(target, exp.Schema) and not tree.find(exp.LikeProperty):
        return False
    return True


def _is_complete_create(text):
    # Import lazily: parser exposes prepare_sql while its dialect is defined in
    # that same module. Invoke the dialect directly, never the preprocessing
    # entry point, when deciding whether the original SQL was already valid.
    from .parser import CompatiblePostgres

    class QuietParser(CompatiblePostgres.Parser):
        def _warn_unsupported(self):
            pass

    try:
        dialect = CompatiblePostgres()
        trees = QuietParser(dialect=dialect).parse(dialect.tokenize(text), text)
        trees = [tree for tree in trees if tree is not None]
        return len(trees) == 1 and _complete_table_creation(trees[0])
    except Exception:
        return False


def _distribution_is_removable(tokens, suffix, text):
    # Unreserved words can be actual source names or projected identifiers:
    # FROM audit.distributed randomly / SELECT distributed randomly. Preserve
    # every already-valid PostgreSQL CREATE even when the suffix looks exactly
    # like a Greenplum clause. A qualified name can never begin such a clause.
    previous = tokens[len(tokens) - len(suffix) - 1]
    if previous.token_type in {TokenType.DOT, TokenType.COMMA, TokenType.ALIAS}:
        return False
    original = text[tokens[0].start:tokens[-1].end + 1]
    if _is_complete_create(original):
        return False
    prefix = text[tokens[0].start:suffix[0].start]
    # DATE literals and CASE log artifacts are checked through the same
    # compatibility rules as production parsing, after the candidate is gone.
    from .parser import compatible_postgres
    try:
        prefix = compatible_postgres(prefix)
    except Exception:
        return False
    return _is_complete_create(prefix)


def _distribution_tokens(tokens, text):
    """Return only a terminal, balanced, top-level CREATE TABLE distribution.

    Comments are absent from the token stream; their source bytes are retained
    when the clause's tokens are blanked. No matching crosses a semicolon.
    """
    if not _create_table(tokens):
        return []
    depth, depths = 0, []
    for token in tokens:
        depths.append(depth)
        if token.token_type == TokenType.L_PAREN:
            depth += 1
        elif token.token_type == TokenType.R_PAREN:
            depth -= 1
            if depth < 0:
                return []
    if depth:
        return []
    for index in range(len(tokens) - 2, 2, -1):
        if depths[index] or not _word(tokens[index], "DISTRIBUTED"):
            continue
        suffix = tokens[index:]
        if len(suffix) == 2 and any(_word(suffix[1], word) for word in ("RANDOMLY", "REPLICATED")):
            return suffix if _distribution_is_removable(tokens, suffix, text) else []
        if (len(suffix) >= 5 and _word(suffix[1], "BY")
                and suffix[2].token_type == TokenType.L_PAREN
                and suffix[-1].token_type == TokenType.R_PAREN
                and depths[-1] == 1
                and all(level >= 1 for level in depths[index + 3:])):
            # BY takes identifier columns, not arbitrary SQL/function bodies.
            columns = suffix[3:-1]
            return suffix if (_distribution_columns(columns)
                              and _distribution_is_removable(tokens, suffix, text)) else []
        return []
    return []


def _case_zero_end_edits(tokens):
    """Recover only the complete zero result immediately after THEN or ELSE.

    SQLGlot 26 tokenizes 0END as NUMBER(0E), VAR(ND). Repair requires an open
    CASE and a result boundary after END. Identifiers and predicate operands
    such as ``x0END`` or ``WHERE x=0END`` are left untouched.
    """
    edits, cases = [], []
    boundaries = {TokenType.COMMA, TokenType.R_PAREN, TokenType.SEMICOLON}
    for index, token in enumerate(tokens):
        if token.token_type == TokenType.SEMICOLON:
            cases = []
        elif _word(token, "CASE"):
            cases.append(True)
        elif _word(token, "END") and cases:
            cases.pop()
        elif (cases and index and index + 1 < len(tokens)
                and token.token_type == TokenType.NUMBER and token.text.upper() == "0E"
                and tokens[index + 1].token_type == TokenType.VAR
                and tokens[index + 1].text.upper() == "ND"
                and token.end + 1 == tokens[index + 1].start
                and any(_word(tokens[index - 1], word) for word in ("THEN", "ELSE"))):
            after = tokens[index + 2] if index + 2 < len(tokens) else None
            if (after is None or after.token_type in boundaries
                    or any(_word(after, word) for word in (
                        "AS", "FROM", "WHERE", "GROUP BY", "HAVING", "ORDER BY", "END", "ELSE", "WHEN"))):
                edits.append((token.start, tokens[index + 1].end + 1, "0 END"))
                cases.pop()
    return edits


def prepare_sql(text):
    """Return ``(parse_view, notes)`` without quote-encoding assumptions.

    The log format uses ASCII CAN as a newline; decoding it is intentional.
    Distribution affects placement, not table/partition access, and is removed
    only from each CREATE TABLE's terminal token clause. All other source bytes
    (including comments between clause tokens) survive. Ambiguous nonstandard
    CASE comparison syntax is retained instead of guessing NULL semantics.
    """
    prepared, notes = text.replace("\x18", "\n"), []
    if prepared != text:
        notes.append("仅解析时将日志\\x18换行标记还原为换行")
    try:
        tokens = sqlglot.tokenize(prepared, read="postgres")
    except Exception:
        return prepared, notes
    edits, statement = [], []
    for token in tokens + [None]:
        if token is None or token.token_type == TokenType.SEMICOLON:
            removed = _distribution_tokens(statement, prepared)
            if removed:
                edits.extend((item.start, item.end + 1, " " * (item.end + 1 - item.start)) for item in removed)
                if "仅解析时移除CREATE TABLE末尾DISTRIBUTED子句" not in notes:
                    notes.append("仅解析时移除CREATE TABLE末尾DISTRIBUTED子句")
            statement = []
        else:
            statement.append(token)
    zero_edits = _case_zero_end_edits(tokens)
    if zero_edits:
        edits.extend(zero_edits)
        notes.append("仅解析时将CASE完整结果0END分词为0 END")
    for index, token in enumerate(tokens):
        if (_word(token, "WHEN") and index + 4 < len(tokens)
                and _word(tokens[index + 1], "IS") and _word(tokens[index + 2], "NOT")
                and _word(tokens[index + 3], "DISTINCT") and _word(tokens[index + 4], "FROM")):
            notes.append("非标准WHEN IS NOT DISTINCT FROM保持原文，未猜测CASE比较语义")
            break
    for start, end, replacement in sorted(edits, reverse=True):
        prepared = prepared[:start] + replacement + prepared[end:]
    return prepared, notes


def recover_identifier_quote_encoding(text):
    """Return a diagnostic recovery candidate, or None when ambiguous.

    Call only after normal parsing fails. A candidate is an entire quoted
    identifier framed by exactly two quotes on both sides: ``""column""``.
    Every such malformed frame must be complete and consistent. Ordinary
    quoted identifiers (including their internal doubled quote), single/dollar
    strings, and nested comments are opaque. The body may contain Unicode,
    spaces, punctuation or start with digits, as quoted SQL identifiers may.
    Empty/NUL bodies and encoded identifiers containing quotes are ambiguous
    and are not decoded; in particular, a run of three/four quotes is never
    treated as a two-quote frame boundary.
    The caller must retain raw SQL, mark the assumption and validate its AST.
    """
    edits, cursor = [], 0
    while cursor < len(text):
        end = _skip_comment(text, cursor)
        if end is None:
            return None
        if end != cursor:
            cursor = end
            continue
        char = text[cursor]
        if char == "'":
            # PostgreSQL ordinary strings use doubled apostrophes; only E'...'
            # makes backslashes escapes. In particular ESCAPE '\' is complete.
            escaped = (cursor > 0 and text[cursor - 1] in "Ee"
                       and (cursor < 2 or not (text[cursor - 2].isalnum() or text[cursor - 2] in "_$")))
            end = cursor + 1
            while end < len(text):
                if escaped and text[end] == "\\":
                    end += 2
                elif text[end] == "'":
                    if end + 1 < len(text) and text[end + 1] == "'":
                        end += 2
                    else:
                        break
                else:
                    end += 1
            if end >= len(text):
                return None
            cursor = end + 1
            continue
        if char == "$" and (not cursor or not (text[cursor - 1].isalnum() or text[cursor - 1] in "_$")):
            match = _DOLLAR_DELIMITER.match(text, cursor)
            if match:
                end = text.find(match.group(), match.end())
                if end < 0:
                    return None
                cursor = end + len(match.group())
                continue
        if char == '"':
            if text.startswith('""', cursor) and not text.startswith('"""', cursor):
                end = text.find('"', cursor + 2)
                body = text[cursor + 2:end] if end >= 0 else ""
                if (not body or "\x00" in body
                        or not text.startswith('""', end) or text.startswith('"""', end)
                        or cursor and (text[cursor - 1].isalnum() or text[cursor - 1] in '_$"')
                        or end + 2 < len(text) and (text[end + 2].isalnum() or text[end + 2] in '_$"')):
                    return None
                edits.append((cursor, end + 2, '"' + body + '"'))
                cursor = end + 2
                continue
            # Normal quoted identifier, including legal escaped internal quotes.
            end = cursor + 1
            while end < len(text):
                if text[end] == '"':
                    if end + 1 < len(text) and text[end + 1] == '"':
                        end += 2
                    else:
                        break
                else:
                    end += 1
            if end >= len(text):
                return None
            cursor = end + 1
            continue
        cursor += 1
    if not edits:
        return None
    for start, end, replacement in reversed(edits):
        text = text[:start] + replacement + text[end:]
    return text
