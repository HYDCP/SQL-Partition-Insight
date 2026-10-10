"""Independently detect missing, definite SQL partition-access evidence.

This checker reads the original SQL and the final outputs. It deliberately
does not consume the analyzer's tables, predicates, scopes or constant values.
It covers a conservative subset, not verified physical partition accesses.
Unsupported syntax and uncertain coercions are counted as coverage gaps.
"""
import collections
import datetime
from decimal import Decimal, InvalidOperation
import re

import sqlglot
from sqlglot import exp
from sqlglot.dialects.postgres import Postgres
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
from sqlglot.optimizer.scope import Scope, traverse_scope
from sqlglot.tokens import TokenType

CHECKER_VERSION = 1
_EXCEPTION = re.compile(
    r'^\s*An exception was encountered during the execution of statement:', re.I)
_SQL_WORDS = {'SELECT', 'WITH', 'INSERT', 'UPDATE', 'DELETE', 'CREATE',
              'ALTER', 'COPY', 'TRUNCATE', 'DROP', 'ANALYZE', 'COMMENT'}
_INTEGER_LIMITS = {exp.DataType.Type.SMALLINT: 2 ** 15,
                   exp.DataType.Type.INT: 2 ** 31,
                   exp.DataType.Type.BIGINT: 2 ** 63}


class _AuditPostgres(Postgres):
    # Greenplum permits VALUES as a relation alias. Keep VALUES constructors
    # intact; the stock PostgreSQL identifier rules handle column positions.
    class Parser(Postgres.Parser):
        ALIAS_TOKENS = Postgres.Parser.ALIAS_TOKENS | {TokenType.VALUES}
        TABLE_ALIAS_TOKENS = Postgres.Parser.TABLE_ALIAS_TOKENS | {TokenType.VALUES}

        def _parse_alias(self, this, explicit=False):
            if this is None and self._curr and self._curr.token_type == TokenType.VALUES:
                return None
            return super()._parse_alias(this, explicit=explicit)


def _word(token, text):
    return (token.token_type not in (TokenType.IDENTIFIER, TokenType.STRING,
                                    TokenType.HEREDOC_STRING)
            and token.text.upper() == text)


def _split(text):
    tokens = sqlglot.tokenize(text, read='postgres')
    parts, start = [], 0
    for token in tokens:
        if token.token_type == TokenType.SEMICOLON:
            part = text[start:token.start].strip()
            if part and sqlglot.tokenize(part, read='postgres'):
                parts.append(part)
            start = token.end + 1
    tail = text[start:].strip()
    if tail and sqlglot.tokenize(tail, read='postgres'):
        parts.append(tail)
    return parts


def _parse_statement(text):
    """Prefer the complete stock AST; independently tolerate a terminal GP clause."""
    try:
        tree = sqlglot.parse_one(text, read=_AuditPostgres)
        if tree is not None and not isinstance(tree, exp.Command):
            return normalize_identifiers(tree, dialect='postgres')
    except sqlglot.errors.ParseError:
        tree = None
    tokens = sqlglot.tokenize(text, read='postgres')
    if not tokens or not _word(tokens[0], 'CREATE'):
        raise ValueError('unsupported_statement')
    depth, candidates = 0, []
    for index, token in enumerate(tokens):
        if token.token_type == TokenType.L_PAREN:
            depth += 1
        elif token.token_type == TokenType.R_PAREN:
            depth -= 1
        elif depth == 0 and _word(token, 'DISTRIBUTED'):
            if index and tokens[index - 1].token_type != TokenType.DOT:
                candidates.append(index)
    if not candidates:
        raise ValueError('unsupported_statement')
    index = candidates[-1]
    suffix = tokens[index + 1:]
    simple = (len(suffix) == 1 and any(_word(suffix[0], word)
                                     for word in ('RANDOMLY', 'REPLICATED')))
    by = (len(suffix) >= 4 and _word(suffix[0], 'BY')
          and suffix[1].token_type == TokenType.L_PAREN
          and suffix[-1].token_type == TokenType.R_PAREN)
    if by:
        # A distribution key list consists of identifiers separated by commas.
        # Do not discard another parenthesized clause following that list.
        names = suffix[2:-1]
        by = len(names) % 2 == 1 and all(
            (token.token_type == TokenType.COMMA if offset % 2 else
             token.token_type == TokenType.IDENTIFIER or
             token.token_type != TokenType.STRING and
             re.fullmatch(r'[^\W\d][\w$]*', token.text, re.UNICODE) is not None)
            for offset, token in enumerate(names))
    if not (simple or by):
        raise ValueError('unsupported_statement')
    candidate = normalize_identifiers(
        sqlglot.parse_one(text[:tokens[index].start], read=_AuditPostgres),
        dialect='postgres')
    if not isinstance(candidate, exp.Create) or candidate.args.get('expression') is None:
        raise ValueError('unsupported_statement')
    return candidate


def _unparen(node):
    while isinstance(node, exp.Paren):
        node = node.this
    return node


def _date(text):
    if re.fullmatch(r'\d{8}', text):
        text = '{}-{}-{}'.format(text[:4], text[4:6], text[6:])
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', text):
        return None
    try:
        return datetime.datetime.strptime(text, '%Y-%m-%d').date().isoformat()
    except ValueError:
        return None


def _number(text):
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    if not _numeric_storage_ok(value):
        return None
    rendered = format(value, 'f')
    if '.' in rendered:
        rendered = rendered.rstrip('0').rstrip('.')
    return '0' if value == 0 else rendered


def _numeric_storage_ok(value):
    """An unconstrained PostgreSQL NUMERIC still has storage boundaries.

    PostgreSQL numeric.c uses a 14-bit display scale and a signed int16
    base-10000 weight. Check the short Decimal exponent before rendering.
    https://github.com/postgres/postgres/blob/REL_12_STABLE/src/backend/utils/adt/numeric.c
    """
    if not value.is_finite():
        return False
    exponent = value.as_tuple().exponent
    if max(0, -exponent) > 16383:
        return False
    if value.is_zero():
        # Avoid assuming arbitrary positive exponent input is accepted. Zero
        # has no physical weight, so this is a deliberate coverage restriction.
        return exponent <= 131071
    return value.adjusted() <= 131071


def _explicit_integer_limit(node):
    """Unary minus preserves the width of an explicitly typed integer input."""
    node = _unparen(node)
    if isinstance(node, exp.Neg):
        return _explicit_integer_limit(node.this)
    if type(node) is exp.Cast:
        target = node.args.get('to')
        if isinstance(target, exp.DataType) and not target.expressions:
            return _INTEGER_LIMITS.get(target.this)
    return None


def _scalar(node):
    """Evaluate only explicit scalar semantics, retaining numeric display scale.

    The Decimal quantum is needed by numeric -> TEXT. Partition identity
    normalization is separate and must never erase that intermediate scale.
    """
    node = _unparen(node)
    if isinstance(node, exp.Literal):
        if node.is_string:
            return ('string', node.this)
        if not re.fullmatch(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?', node.this):
            return None
        try:
            value = Decimal(node.this)
        except InvalidOperation:
            return None
        return ('number', value) if _numeric_storage_ok(value) else None
    if isinstance(node, exp.Neg):
        value = _scalar(node.this)
        if value and value[0] == 'number':
            # Unary minus must not round a SQL literal using Python's decimal
            # arithmetic context (whose default precision is only 28 digits).
            result = value[1].copy_negate()
            limit = _explicit_integer_limit(node.this)
            if limit is not None and not -limit <= result < limit:
                return None
            return ('number', result)
        return None
    if type(node) is exp.Cast:
        kind = node.args.get('to')
        if (not isinstance(kind, exp.DataType) or kind.expressions
                or any(node.args.get(key) for key in ('format', 'safe', 'action', 'default'))):
            return None
        literal_text = isinstance(node.this, exp.Literal) and node.this.is_string
        if kind.this == exp.DataType.Type.DATE and literal_text:
            value = _date(node.this.this)
            return ('date', value) if value else None
        if kind.this == exp.DataType.Type.TIMESTAMP and literal_text:
            text = node.this.this
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?', text):
                return None
            try:
                value = datetime.datetime.fromisoformat(text)
            except ValueError:
                return None
            return ('timestamp', value.isoformat(sep=' '))
        child = _scalar(node.this)
        if child is None:
            return None
        limits = _INTEGER_LIMITS
        if kind.this in limits:
            if child[0] == 'string':
                text = child[1].strip(' \t\r\n\f\v')
                if not re.fullmatch(r'[+-]?[0-9]+', text):
                    return None
                decimal = Decimal(text)
            elif child[0] == 'number':
                decimal = child[1]
            else:
                return None
            limit = limits[kind.this]
            if not -limit <= decimal < limit:
                return None
            number = int(decimal)
            if Decimal(number) != decimal:
                # numeric -> integer rounding is deliberately unproved.
                return None
            return ('number', Decimal(number))
        if kind.this == exp.DataType.Type.DECIMAL:
            if child[0] == 'number':
                return child
            if child[0] == 'string':
                text = child[1].strip(' \t\r\n\f\v')
                if re.fullmatch(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?', text):
                    try:
                        value = Decimal(text)
                    except InvalidOperation:
                        return None
                    return ('number', value) if _numeric_storage_ok(value) else None
            return None
        if kind.this in (exp.DataType.Type.TEXT, exp.DataType.Type.VARCHAR):
            if child[0] == 'string':
                return child
            if child[0] == 'number':
                # PostgreSQL numeric output retains dscale but normalizes the
                # sign of zero. Float/date/time conversions are not accepted.
                value = child[1].copy_abs() if child[1].is_zero() else child[1]
                return ('string', format(value, 'f'))
    # Functions, interval arithmetic, casts of untyped columns and session
    # dependent date/time conversions intentionally remain unproved.
    return None


def _constant(node):
    """Reliable typed partition identity, without guessing a column's type."""
    result = _scalar(node)
    if result is None:
        return None
    if result[0] == 'number':
        return ('number', _number(str(result[1])))
    return result


def _table_name(table):
    return '.'.join(part.name for part in table.parts)


def _owner(node):
    return node.find_ancestor(exp.Select, exp.Delete, exp.Update)


def _visible(table, column):
    if not column.table:
        return not column.db and not column.catalog
    if table.alias:
        return column.table == table.alias and not column.db and not column.catalog
    return (column.table == table.name and (not column.db or column.db == table.db)
            and (not column.catalog or column.catalog == table.catalog))


def _local_sources(node):
    # Include derived/CTE references in ambiguity checks. They cannot be
    # assigned a base-table predicate by this checker.
    relations = []
    source = node.args.get('from')
    if source is not None:
        relations.extend([source.this] + list(source.expressions))
    relations.extend(join.this for join in node.args.get('joins', []))
    if isinstance(node, (exp.Update, exp.Delete)):
        relations.insert(0, node.this)
        using = node.args.get('using')
        if using:
            relations.extend(using if isinstance(using, list) else [using])
    return [relation for relation in relations if relation is not None]


def _resolve(node, column, tables):
    sources = _local_sources(node)
    if not column.table:
        if len(sources) != 1 or column.db or column.catalog:
            return None
        return sources[0] if id(sources[0]) in tables else None
    found = [source for source in sources if isinstance(source, exp.Table)
             and _visible(source, column)]
    if len(found) == 1 and id(found[0]) in tables:
        return found[0]
    return None


def _atoms(node):
    node = _unparen(node)
    if isinstance(node, exp.And):
        return _atoms(node.this) + _atoms(node.expression)
    return [node]


def _finite(node, field):
    node = _unparen(node)
    if isinstance(node, exp.EQ):
        column, literal = node.this, node.expression
        if not isinstance(column, exp.Column):
            column, literal = literal, column
        if not isinstance(column, exp.Column) or column.name != field:
            return None
        value = _constant(literal)
        return [value] if value is not None else None
    if isinstance(node, exp.In) and isinstance(node.this, exp.Column) and node.this.name == field:
        if node.args.get('query') or not node.expressions:
            return None
        values = [_constant(value) for value in node.expressions]
        if any(value is None for value in values):
            return None
        kinds = {value[0] for value in values}
        if kinds == {'date', 'string'}:
            # An explicitly typed DATE fixes the common IN-list input type.
            converted = []
            for kind, text in values:
                value = text if kind == 'date' else _date(text)
                if value is None:
                    return None
                converted.append(('date', value))
            values = converted
        elif len(kinds) > 1:
            return None
        return values
    return None


def _outer_filter(node):
    # A consumer filter may be combined by the main lineage implementation
    # with a local condition. We do not guess its coercions or lineage.
    parent = node.parent
    while parent is not None:
        if isinstance(parent, exp.Select) and (
                parent.args.get('where') or parent.args.get('having')
                or any(join.args.get('on') for join in parent.args.get('joins', []))):
            return True
        parent = parent.parent
    return False


def _update_tuple_values(node):
    """A positional UPDATE source must prove exactly one constant row."""
    while isinstance(node, (exp.Paren, exp.Subquery)):
        if any(value for key, value in node.args.items() if key != 'this'):
            return None
        node = node.this
    if isinstance(node, exp.Tuple):
        projections = node.expressions
    elif isinstance(node, exp.Select):
        # A bare scalar SELECT has exactly one row. Reject every query modifier
        # rather than trying to evaluate row counts, aggregates or correlation.
        if any(value for key, value in node.args.items() if key != 'expressions'):
            return None
        projections = node.expressions
    elif isinstance(node, exp.Values):
        if (len(node.expressions) != 1 or not isinstance(node.expressions[0], exp.Tuple)
                or any(value for key, value in node.args.items() if key != 'expressions')):
            return None
        projections = node.expressions[0].expressions
    else:
        return None
    if not projections:
        return None
    values = [_constant(value.this if isinstance(value, exp.Alias) else value)
              for value in projections]
    return values if all(value is not None for value in values) else None


def _ddl_signature(text):
    """Recognize a complete explicit partition object, using tokens only."""
    tokens = sqlglot.tokenize(text, read='postgres')
    if len(tokens) < 3 or tokens[0].text.upper() not in {'ADD', 'DROP', 'TRUNCATE', 'EXCHANGE', 'RENAME'}:
        return None
    action = tokens[0].text.upper()
    index = 1
    if _word(tokens[index], 'DEFAULT'):
        index += 1
    if index >= len(tokens) or not _word(tokens[index], 'PARTITION'):
        return None
    index += 1
    if index >= len(tokens):
        return None
    if _word(tokens[index], 'FOR'):
        index += 1
        if index >= len(tokens) or tokens[index].token_type != TokenType.L_PAREN:
            return None
        depth, end = 0, None
        for offset in range(index, len(tokens)):
            token = tokens[offset]
            depth += token.token_type == TokenType.L_PAREN
            depth -= token.token_type == TokenType.R_PAREN
            if depth == 0:
                end = offset
                break
        if end is None:
            return None
        body = text[tokens[index].end + 1:tokens[end].start]
        try:
            values = sqlglot.parse_one('SELECT ' + body, read='postgres').expressions
        except sqlglot.errors.ParseError:
            return None
        values = [_constant(value) for value in values]
        if not values or any(value is None for value in values):
            return None
        return (action, 'partition_for', tuple(values))
    token = tokens[index]
    if token.token_type in (TokenType.STRING, TokenType.HEREDOC_STRING,
                            TokenType.L_PAREN, TokenType.R_PAREN, TokenType.DOT):
        return None
    if not token.text or (token.token_type != TokenType.IDENTIFIER and
                          not re.fullmatch(r'[^\W\d][\w$]*', token.text, re.UNICODE)):
        return None
    name = token.text if token.token_type == TokenType.IDENTIFIER else token.text.lower()
    return (action, 'partition_name', name)


def _alter(text, ordinal, config, skips):
    tokens = sqlglot.tokenize(text, read='postgres')
    if len(tokens) < 4 or not _word(tokens[0], 'ALTER') or not _word(tokens[1], 'TABLE'):
        return None
    index = 2
    if _word(tokens[index], 'IF'):
        return None
    if _word(tokens[index], 'ONLY'):
        index += 1
    first = index
    index += 1
    while index + 1 < len(tokens) and tokens[index].token_type == TokenType.DOT:
        index += 2
    name = text[tokens[first].start:tokens[index - 1].end + 1]
    try:
        table = normalize_identifiers(sqlglot.parse_one('SELECT * FROM ' + name, read='postgres'), dialect='postgres').find(exp.Table)
    except sqlglot.errors.ParseError:
        return None
    if table is None or not isinstance(table.this, exp.Identifier):
        return None
    ordinal += 1
    entry = config.get(_table_name(table)) if table.db else None
    if not entry or len(entry.get('partition_columns', [])) != 1:
        skips['not_configured_single_field'] += 1
        return [], ordinal
    action = text[tokens[index].start:] if index < len(tokens) else ''
    signature = _ddl_signature(action)
    if signature is None:
        skips['unsupported_partition_ddl'] += 1
        return [], ordinal
    role = ('ALTER新增分区' if signature[0] == 'ADD' else
            'ALTER清空分区' if signature[0] == 'TRUNCATE' else 'ALTER分区维护')
    expectation = {'table': _table_name(table), 'occurrence': ordinal,
                   'reference': table.alias_or_name, 'partition_field': entry['partition_columns'][0],
                   'expected_role': role, 'partition_value': {'type': signature[1], 'value': signature[2]},
                   '_signature': signature, 'evidence': action, 'origin': 'ALTER TABLE'}
    return [expectation], ordinal


def _expectations(text, config):
    expected, skips, ordinal = [], collections.Counter(), 0
    if _EXCEPTION.match(text):
        return [], {'exception_execution_log': 1}
    text = text.replace('\x18', '\n')
    try:
        statements = _split(text)
    except Exception:
        return [], {'unparseable_original_sql': 1}
    if not statements:
        return [], {'empty_sql': 1}
    for statement_number, statement in enumerate(statements, 1):
        try:
            tokens = sqlglot.tokenize(statement, read='postgres')
            if not tokens or tokens[0].text.upper() not in _SQL_WORDS:
                skips['non_sql_statement'] += 1
                continue
            if _word(tokens[0], 'ALTER'):
                result = _alter(statement, ordinal, config, skips)
                if result is not None:
                    items, ordinal = result
                    for item in items:
                        item['statement'] = statement_number
                    expected.extend(items)
                    continue
            tree = _parse_statement(statement)
            if not isinstance(tree, (exp.Select, exp.SetOperation, exp.Insert, exp.Update,
                                     exp.Delete, exp.Create, exp.Copy, exp.Drop,
                                     exp.TruncateTable, exp.Analyze)):
                # Some stock DDL ASTs represent relations as Columns, while
                # the main extractor counts them as table references. Do not
                # guess subsequent occurrence numbers from such a statement.
                skips['unsupported_statement_occurrence_alignment'] += 1
                break
            scopes = list(traverse_scope(tree))
        except Exception:
            skips['unparseable_original_sql'] += 1
            # Occurrence numbers after an unsupported statement cannot be
            # safely aligned with final output; stop this context here.
            break
        cte_ids = {id(table) for scope in scopes for table in scope.tables
                   if isinstance(scope.sources.get(table.alias_or_name), Scope)}
        references = [table for table in tree.find_all(exp.Table)
                      if id(table) not in cte_ids and isinstance(table.this, exp.Identifier)]
        details = {}
        target = tree.this
        if isinstance(target, exp.Schema):
            target = target.this
        for table in references:
            ordinal += 1
            role = '查询读取'
            if table is target:
                role = {exp.Insert: 'INSERT写入', exp.Update: 'UPDATE更新', exp.Delete: 'DELETE删除',
                        exp.Create: 'CTAS写入'}.get(type(tree), 'unsupported')
            entry = config.get(_table_name(table)) if table.db else None
            if not entry or len(entry.get('partition_columns', [])) != 1:
                skips['not_configured_single_field'] += 1
                continue
            if isinstance(tree, exp.Copy):
                skips['copy_body_not_recorded'] += 1
                continue
            if role == 'unsupported':
                skips['unsupported_table_operation'] += 1
                continue
            details[id(table)] = {'table': _table_name(table), 'occurrence': ordinal,
                'reference': table.alias_or_name, 'partition_field': entry['partition_columns'][0],
                'expected_role': role, 'statement': statement_number}

        def add(table, values, evidence, origin):
            detail = details.get(id(table))
            if detail is None:
                return
            for value in values:
                item = dict(detail, partition_value={'type': value[0], 'value': value[1]},
                            _value=value, evidence=evidence, origin=origin)
                expected.append(item)

        for node in tree.walk():
            if not isinstance(node, (exp.Select, exp.Update, exp.Delete)):
                continue
            local = [table for table in references if _owner(table) is node]
            local_ids = {id(table) for table in local}
            condition = node.args.get('where')
            if condition is None:
                if any(id(table) in details for table in local):
                    reason = ('non_where_conditions_not_checked'
                              if node.args.get('having') or any(join.args.get('on') for join in node.args.get('joins', []))
                              else 'no_local_where_partition_constraint')
                    skips[reason] += 1
                continue
            if _outer_filter(node):
                skips['consumer_filter_lineage_not_checked'] += 1
                continue
            if any(join.side or join.kind.upper() in {'ANTI', 'SEMI'}
                   for join in node.args.get('joins', [])):
                skips['outer_or_semi_join_not_checked'] += 1
                continue
            if any(isinstance(item, (exp.Or, exp.Not)) and _owner(item) is node
                   for item in condition.walk()):
                skips['or_not_condition'] += 1
                continue
            constraints = collections.defaultdict(list)
            containers = [condition]
            if node.args.get('having') is not None:
                containers.append(node.args['having'])
            containers.extend(join.args['on'] for join in node.args.get('joins', []) if join.args.get('on'))
            for container in containers:
                body = container.this if isinstance(container, (exp.Where, exp.Having)) else container
                for atom in _atoms(body):
                    cols = [column for column in atom.find_all(exp.Column) if _owner(column) is node]
                    touched = set()
                    for column in cols:
                        table = _resolve(node, column, local_ids)
                        detail = details.get(id(table)) if table is not None else None
                        if detail and column.name == detail['partition_field']:
                            touched.add(id(table))
                    for table_id in touched:
                        constraints[table_id].append((atom, container is condition))
            for table in local:
                detail = details.get(id(table))
                if detail is None:
                    continue
                items = constraints[id(table)]
                if not items:
                    skips['no_local_partition_constraint'] += 1
                    continue
                if len(items) != 1 or not items[0][1]:
                    skips['multiple_or_non_where_partition_constraints'] += 1
                    continue
                atom = items[0][0]
                if table.args.get('sample'):
                    skips['tablesample_not_checked'] += 1
                    continue
                values = _finite(atom, detail['partition_field'])
                if values is None:
                    skips['non_finite_or_unproved_partition_condition'] += 1
                    continue
                # A finite atom must have exactly its direct partition column.
                columns = list(atom.find_all(exp.Column))
                if len(columns) != 1 or _resolve(node, columns[0], local_ids) is not table:
                    skips['ambiguous_partition_column'] += 1
                    continue
                add(table, values, atom.sql(dialect='postgres'), 'WHERE')

        target_detail = details.get(id(target))
        if target_detail and isinstance(tree, exp.Insert):
            schema, rows = tree.this, tree.args.get('expression')
            if isinstance(schema, exp.Schema) and isinstance(rows, exp.Values):
                fields = [column.name for column in schema.expressions]
                field = target_detail['partition_field']
                if fields.count(field) == 1:
                    index = fields.index(field)
                    for row in rows.expressions:
                        if not isinstance(row, exp.Tuple) or len(row.expressions) != len(fields):
                            skips['invalid_values_row_arity'] += 1
                            continue
                        value = _constant(row.expressions[index])
                        if value is None:
                            skips['unproved_insert_value'] += 1
                        else:
                            add(target, [value], row.expressions[index].sql(dialect='postgres'), 'INSERT VALUES')
                else:
                    skips['insert_target_partition_column_missing'] += 1
            else:
                skips['insert_without_explicit_values_columns'] += 1
        elif target_detail and isinstance(tree, exp.Update):
            for assignment in tree.expressions:
                column = assignment.this if isinstance(assignment, exp.EQ) else None
                if isinstance(column, exp.Tuple):
                    columns = column.expressions
                    names = [item.name for item in columns if isinstance(item, exp.Column)]
                    if (len(names) != len(columns) or len(set(names)) != len(names)
                            or any(item.table or item.db or item.catalog for item in columns)):
                        skips['unproved_update_tuple_targets'] += 1
                        continue
                    if target_detail['partition_field'] not in names:
                        continue
                    values = _update_tuple_values(assignment.expression)
                    if values is None or len(values) != len(columns):
                        skips['unproved_update_tuple_source'] += 1
                        continue
                    index = names.index(target_detail['partition_field'])
                    add(target, [values[index]], assignment.sql(dialect='postgres'), 'UPDATE SET')
                    continue
                if not isinstance(column, exp.Column) or column.name != target_detail['partition_field']:
                    continue
                value = _constant(assignment.expression)
                if value is None:
                    skips['unproved_update_assignment'] += 1
                else:
                    add(target, [value], assignment.sql(dialect='postgres'), 'UPDATE SET')
    unique = {}
    for item in expected:
        key = (item['statement'], item['occurrence'], item['table'], item['expected_role'],
               item.get('_value'), item.get('_signature'))
        unique.setdefault(key, item)
    return list(unique.values()), dict(skips)


def _identity_values(identity):
    if not isinstance(identity, (tuple, list)) or not identity:
        return set()
    if identity[0] == 'values' and len(identity) == 2:
        return {tuple(value) for value in identity[1]
                if isinstance(value, (tuple, list)) and len(value) == 3}
    if identity[0] == 'mixed' and len(identity) > 1:
        return {tuple(value) for option in identity[1] for value in option
                if isinstance(value, (tuple, list)) and len(value) == 3}
    if identity[0] == 'assignment' and len(identity) > 1:
        return _identity_values(identity[1])
    return set()


def _has_expected(row, item):
    identity = row.get('partition_identity')
    if '_value' in item:
        kind, value = item['_value']
        return (item['partition_field'], kind, value) in _identity_values(identity)
    if isinstance(identity, (tuple, list)) and len(identity) == 2 and identity[0] == 'partition_ddl':
        try:
            return _ddl_signature(identity[1]) == item['_signature']
        except Exception:
            return False
    return False


def check_partition_omissions(profile, report, progress=None):
    """Return warnings plus honest coverage, independently of the analyzer.

    ``progress(done, total)`` is called every 1,000 SQL/user contexts and at
    completion. Raw SQL is never changed. No whole-run AST cache is retained.
    Both per-reference mapping and final statistical groups must cover a
    supported expectation. Counts describe SQL evidence, not physical I/O.
    """
    records = profile.get('sqls', [])
    config = report.get('partition_config', {}).get('tables', {})
    mappings = collections.defaultdict(list)
    for row in report.get('mapping', []):
        mappings[(row.get('id'), row.get('occurrence'), row.get('table'))].append(row)
    groups = collections.defaultdict(list)
    for row in report.get('results', []):
        for sid in row.get('ids', []):
            groups[(sid, row.get('table'))].append(row)
    alerts, skips = [], collections.Counter()
    covered_contexts = covered_logs = expectations_count = 0
    # Only reuse the immediately preceding SQL's finite evidence. This helps
    # adjacent user contexts without holding SQLGlot ASTs or all source texts.
    previous_sql, previous_result = None, None
    for done, record in enumerate(records, 1):
        raw = record.get('sql', '')
        if raw == previous_sql:
            expected, reasons = previous_result
        else:
            expected, reasons = _expectations(raw, config)
            previous_sql, previous_result = raw, (expected, reasons)
        skips.update(reasons)
        if expected:
            covered_contexts += 1
            covered_logs += record.get('count', 0)
        expectations_count += len(expected)
        for item in expected:
            observed = mappings[(record['id'], item['occurrence'], item['table'])]
            matching = [row for row in observed if row.get('role') == item['expected_role'] and _has_expected(row, item)]
            statistical = [row for row in groups[(record['id'], item['table'])]
                           if row.get('role') == item['expected_role'] and _has_expected(row, item)]
            missing = []
            if not matching:
                missing.append('mapping')
            if not statistical:
                missing.append('results')
            if not missing:
                continue
            public = {key: value for key, value in item.items() if not key.startswith('_')}
            alerts.append(dict(public, severity='warning', sql_id=record['id'],
                user_name=record.get('user_name', ''), log_count=record.get('count', 0),
                first_record=record.get('first_record'),
                first_time=record.get('first_time', ''), last_time=record.get('last_time', ''),
                sql=raw, missing_outputs=missing,
                reason='独立识别出明确分区证据，但最终分区访问输出缺少对应表引用、取值或访问类型',
                observed_mappings=observed[:20], observed_mapping_count=len(observed),
                observed_result_count=len(groups[(record['id'], item['table'])])))
        if progress and (done % 1000 == 0 or done == len(records)):
            progress(done, len(records))
    alert_ids = {item['sql_id'] for item in alerts}
    metrics = {'sql_contexts': len(records), 'source_logs': sum(record.get('count', 0) for record in records),
               'supported_sql_contexts': covered_contexts, 'supported_source_logs': covered_logs,
               'expected_accesses': expectations_count, 'alerts': len(alerts),
               'alert_sql_contexts': len(alert_ids),
               'alert_source_logs': sum(record.get('count', 0) for record in records if record['id'] in alert_ids),
               'contexts_without_supported_expectations': len(records) - covered_contexts,
               'skip_reason_counts': dict(sorted(skips.items()))}
    return {'checker_version': CHECKER_VERSION, 'partition_scope': 'sql_evidence',
            'coverage': {'supported': ['本地 WHERE 单字段等值和有限 IN', '显式目标列 INSERT VALUES',
                        'UPDATE 静态分区赋值及无来源单行元组赋值', 'ALTER 明确分区对象',
                        '安全整数/NUMERIC/TEXT 标量 CAST'],
                         'limitations': ['不证明物理分区或执行成功', '不下推派生表或 CTE 消费者条件',
                            '未覆盖动态、范围、OR/NOT、多重约束、COPY 正文、复杂类型转换',
                            '解析失败计入覆盖缺口；空告警不等于全部 SQL 正确']},
            'metrics': metrics, 'alerts': alerts}
