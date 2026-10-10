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

from .lexical import LexicalPostgres, tokenize_postgres

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


class CompatiblePostgres(LexicalPostgres):
    """Preserve PostgreSQL tokens, aliases and Greenplum CUSTOM format evidence.

    Columns are already handled by PostgreSQL's VALUES_FOLLOWED_BY_PAREN rule.
    Do not add VALUES to generic ID_VAR_TOKENS: the statement/expression parser
    must still recognize VALUES row constructors and INSERT ... VALUES.
    External table options use native AST nodes for analysis; rendering them
    with the PostgreSQL generator is not executable Greenplum DDL translation.
    """
    class Parser(Postgres.Parser):
        ALIAS_TOKENS = Postgres.Parser.ALIAS_TOKENS | {TokenType.VALUES}
        TABLE_ALIAS_TOKENS = Postgres.Parser.TABLE_ALIAS_TOKENS | {
            TokenType.VALUES, TokenType.APPLY}
        UPDATE_ALIAS_TOKENS = Postgres.Parser.UPDATE_ALIAS_TOKENS | {TokenType.APPLY}
        # PostgreSQL E'...' is classified as BYTE_STRING by the pinned lexer,
        # although it is a character-string constant in Greenplum's Sconst.
        EXTERNAL_STRING_TOKENS = frozenset(Postgres.Parser.STRING_PARSERS) | {TokenType.BYTE_STRING}
        PROPERTY_PARSERS = dict(Postgres.Parser.PROPERTY_PARSERS)
        PROPERTY_PARSERS['FORMAT'] = lambda self: self._parse_external_format()
        PROPERTY_PARSERS['LOCATION'] = lambda self: self._parse_external_source(exp.LocationProperty)
        PROPERTY_PARSERS['EXECUTE'] = lambda self: self._parse_external_source(exp.ExecuteAsProperty)
        FUNCTIONS = dict(Postgres.Parser.FUNCTIONS)
        # The pinned builders read only arguments[0:2], while their AST classes
        # have optional zone/safe fields that let a third input pass validation.
        for _function_name in ("DATE_TRUNC", "TO_DATE", "TO_TIMESTAMP"):
            FUNCTIONS[_function_name] = _preserve_function_arguments(
                _function_name, Postgres.Parser.FUNCTIONS[_function_name], 2,
                template_index=1 if _function_name in ("TO_DATE", "TO_TIMESTAMP") else None,
            )
        del _function_name

        def _parse_create(self):
            # Activate the Greenplum format grammar only for a complete external
            # table prefix. A query string or an ordinary CREATE property's
            # FORMAT value must keep the pinned PostgreSQL parser behavior.
            start = self._prev
            index = self._index
            tokens = self._tokens
            modifiers = {}
            if (index < len(tokens) and tokens[index].token_type == TokenType.VAR
                    and tokens[index].text.upper() in ('READABLE', 'WRITABLE')):
                modifiers[index] = tokens[index].text.upper()
                index += 1
            external = (index < len(tokens) and tokens[index].token_type == TokenType.VAR
                        and tokens[index].text.upper() == 'EXTERNAL')
            if external:
                index += 1
                if (index < len(tokens) and tokens[index].token_type == TokenType.VAR
                        and tokens[index].text.upper() == 'WEB'):
                    modifiers[index] = 'WEB'
                    index += 1
                if index < len(tokens) and tokens[index].token_type == TokenType.TEMPORARY:
                    index += 1
                external = index < len(tokens) and tokens[index].token_type == TokenType.TABLE
            previous = getattr(self, '_external_table_format', None)
            context = {'modifiers': modifiers, 'formats': [], 'locations': [],
                       'commands': [], 'schemas': [], 'custom': False} if external else None
            self._external_table_format = context
            try:
                tree = super()._parse_create()
                if (context is not None and context['modifiers'] and not context['custom']
                        and isinstance(tree, exp.Create)):
                    # Only CUSTOM has a validated Greenplum grammar here.
                    # Expanding a newly accepted mode/web prefix for another
                    # format would also accept native AS SELECT and invent
                    # accesses inside an illegal external-table CTAS.
                    return self._parse_as_command(start)
                if context is not None and context['custom'] and isinstance(tree, exp.Create):
                    self._validate_external_custom_source(tree, context)
                return tree
            finally:
                self._external_table_format = previous

        def _parse_property(self):
            context = getattr(self, '_external_table_format', None)
            modifier = context['modifiers'].get(self._index) if context is not None else None
            if modifier:
                self._advance()
                # Native generic properties retain external mode/web semantics
                # when the existing PostgreSQL generator renders the AST.
                key = 'EXTERNAL_WEB' if modifier == 'WEB' else 'EXTERNAL_MODE'
                value = exp.Boolean(this=True) if modifier == 'WEB' else exp.Literal.string(modifier)
                return self.expression(exp.Property, this=exp.Var(this=key), value=value)
            return super()._parse_property()

        def _parse_schema(self, this=None):
            start = self._index
            schema = super()._parse_schema(this=this)
            context = getattr(self, '_external_table_format', None)
            if context is not None and isinstance(this, exp.Table):
                context['schemas'].append((start, self._index, self._tokens[start:self._index]))
            return schema

        def _parse_external_source(self, expression_class):
            start = self._index
            source = self._parse_property_assignment(expression_class)
            context = getattr(self, '_external_table_format', None)
            if context is not None:
                key = 'locations' if expression_class is exp.LocationProperty else 'commands'
                context[key].append((start, self._index, self._tokens[start:self._index]))
            return source

        def _validate_external_custom_source(self, tree, context):
            # Validate the consumed source tokens, not just the lossy native
            # tuple AST: SQLGlot otherwise discards LOCATION's trailing comma.
            # Defer until CUSTOM was actually parsed so ordinary PostgreSQL
            # FORMAT/LOCATION and other external formats retain their behavior.
            locations, commands = context['locations'], context['commands']
            if len(context['formats']) != 1:
                self.raise_error('CUSTOM external table requires exactly one FORMAT clause')
            if (not isinstance(tree.this, exp.Schema) or tree.args.get('expression') is not None
                    or len(context['schemas']) != 1):
                self.raise_error('CUSTOM external table requires a field group and has no AS query')
            schema_start, schema_end, schema_tokens = context['schemas'][0]
            if (len(schema_tokens) < 2 or schema_tokens[0].token_type != TokenType.L_PAREN
                    or schema_tokens[-1].token_type != TokenType.R_PAREN):
                self.raise_error('CUSTOM external table field group is incomplete')
            nested = []
            paired = {TokenType.L_PAREN: TokenType.R_PAREN,
                      TokenType.L_BRACKET: TokenType.R_BRACKET,
                      TokenType.L_BRACE: TokenType.R_BRACE}
            field_present = False
            for token in schema_tokens[1:-1]:
                if token.token_type == TokenType.COMMA and not nested:
                    if not field_present:
                        self.raise_error('CUSTOM external field list contains an empty field')
                    field_present = False
                else:
                    if token.token_type in paired:
                        nested.append(paired[token.token_type])
                    elif token.token_type in paired.values():
                        if not nested or nested.pop() != token.token_type:
                            self.raise_error('CUSTOM external field list has unmatched delimiters')
                    field_present = True
            if nested or len(schema_tokens) > 2 and not field_present:
                self.raise_error('CUSTOM external field list is incomplete or has a trailing comma')
            if any(not isinstance(column, exp.ColumnDef)
                   or not isinstance(column.this, exp.Identifier)
                   or not isinstance(column.args.get('kind'), exp.DataType)
                   for column in tree.this.expressions):
                self.raise_error('CUSTOM external fields require complete column names and types')
            if len(locations) == 1 and not commands:
                source_start, source_end, tokens = locations[0]
                if (len(tokens) < 3 or tokens[0].token_type != TokenType.L_PAREN
                        or tokens[-1].token_type != TokenType.R_PAREN):
                    self.raise_error('CUSTOM LOCATION requires a nonempty string list')
                for index, token in enumerate(tokens[1:-1]):
                    if (index % 2 == 0 and token.token_type not in self.EXTERNAL_STRING_TOKENS
                            or index % 2 == 1 and token.token_type != TokenType.COMMA):
                        self.raise_error('CUSTOM LOCATION accepts only comma-separated strings')
                if (len(tokens) - 2) % 2 == 0:
                    self.raise_error('CUSTOM LOCATION does not allow a trailing comma')
            elif (len(commands) == 1 and not locations
                  and 'WEB' in context['modifiers'].values()):
                source_start, source_end, tokens = commands[0]
                if len(tokens) != 1 or tokens[0].token_type not in self.EXTERNAL_STRING_TOKENS:
                    self.raise_error('CUSTOM WEB EXECUTE requires one complete command string')
            else:
                self.raise_error('CUSTOM external table requires one LOCATION or WEB EXECUTE')
            if not (schema_end <= source_start < source_end <= context['formats'][0]):
                self.raise_error('CUSTOM external source must follow fields and precede FORMAT')

        def _parse_external_format(self):
            context = getattr(self, '_external_table_format', None)
            if context is not None:
                context['formats'].append(self._index)
            if not (context is not None and self._curr
                    and self._curr.token_type in self.EXTERNAL_STRING_TOKENS
                    and self._curr.text.upper() == 'CUSTOM'):
                return self._parse_property_assignment(exp.FileFormatProperty)
            context['custom'] = True
            self._advance()
            if not self._match(TokenType.L_PAREN):
                self.raise_error('CUSTOM format requires a formatter option list')
            options = []
            formatter = False
            while True:
                key = self._parse_external_option_identifier()
                if key is None or not self._match(TokenType.EQ):
                    self.raise_error('Expected CUSTOM format option name and =')
                value = self._parse_external_option_value()
                if value is None:
                    self.raise_error('Expected CUSTOM format option value')
                if key.name.lower() == 'formatter':
                    if formatter or not isinstance(value, (exp.Literal, exp.RawString, exp.ByteString,
                                                          exp.National, exp.UnicodeString,
                                                          exp.Identifier, exp.Dot)) or not value.name:
                        self.raise_error('CUSTOM format requires one named formatter')
                    if isinstance(value, exp.Literal) and not value.is_string:
                        self.raise_error('CUSTOM formatter must be a name or string')
                    formatter = True
                options.append(self.expression(exp.Property, this=key, value=value))
                if not self._match(TokenType.COMMA):
                    break
            if not self._match(TokenType.R_PAREN):
                self.raise_error('Expected end of CUSTOM format option list')
            if not formatter:
                self.raise_error('CUSTOM format requires a formatter option')
            # Native nodes retain each option in the AST and remain renderable
            # by the existing PostgreSQL generator. The normalized DDL is an
            # analysis representation, not executable Greenplum SQL.
            return self.expression(exp.FileFormatProperty, this=self.expression(
                exp.Anonymous, this='CUSTOM', expressions=options))

        def _parse_external_option_identifier(self):
            token = self._curr
            if token and (token.token_type == TokenType.IDENTIFIER or
                          (token.token_type not in self.STRING_PARSERS
                           and token.token_type not in self.NUMERIC_PARSERS
                           and re.fullmatch(r'[^\W\d][\w$]*', token.text))):
                self._advance()
                return self.expression(exp.Identifier, this=token.text,
                                       quoted=token.token_type == TokenType.IDENTIFIER)
            return None

        def _parse_external_option_value(self):
            # Greenplum's format_def_item accepts def_arg or a column-name list,
            # not arbitrary SQL expressions. Parse only verified scalar/name
            # forms; arithmetic, calls, subqueries and truncated lists fail.
            if self._curr and self._curr.token_type in self.EXTERNAL_STRING_TOKENS:
                return (self._parse_number() if self._curr.token_type == TokenType.BYTE_STRING
                        else self._parse_string())
            if self._match(TokenType.L_PAREN):
                identifiers = []
                while True:
                    identifier = self._parse_external_option_identifier()
                    if identifier is None:
                        self.raise_error('Expected CUSTOM option column name')
                    identifiers.append(identifier)
                    if not self._match(TokenType.COMMA):
                        break
                if not self._match(TokenType.R_PAREN):
                    self.raise_error('Expected end of CUSTOM option column list')
                return self.expression(exp.Tuple, expressions=identifiers)
            negative = self._match(TokenType.DASH)
            positive = False if negative else self._match(TokenType.PLUS)
            if self._curr and self._curr.token_type == TokenType.NUMBER:
                number = self._parse_number()
                return self.expression(exp.Neg, this=number) if negative else number
            if negative or positive:
                self.raise_error('Expected numeric CUSTOM option value after sign')
            name = self._parse_external_option_identifier()
            if name is not None:
                while self._match(TokenType.DOT):
                    part = self._parse_external_option_identifier()
                    if part is None:
                        self.raise_error('Expected qualified CUSTOM option name')
                    name = self.expression(exp.Dot, this=name, expression=part)
            return name

        def _parse_alias(self, this, explicit=False):
            # _parse_statement first tries an expression before trying SELECT/
            # VALUES. A keyword alias cannot exist without a left expression.
            if this is None and self._curr and self._curr.token_type == TokenType.VALUES:
                return None
            return super()._parse_alias(this, explicit=explicit)


def compatible_postgres(sql):
    sql, _ = prepare_sql(sql)
    tokens = tokenize_postgres(sql)
    edits = []
    for index, token in enumerate(tokens[:-1]):
        literal = tokens[index + 1]
        if literal.token_type != TokenType.STRING:
            continue
        if token.token_type == TokenType.INTERVAL:
            match = _INTERVAL.fullmatch(literal.text.strip())
            if match:
                # A closing quote itself separates a following keyword/alias.
                # Moving the unit outside that quote must retain the boundary:
                # INTERVAL '1 DAY'as date must not become INTERVAL '1' DAYas.
                # One space also preserves non-ASCII identifier boundaries,
                # whose PostgreSQL characters need not satisfy str.isalnum().
                edits.append((literal.start, literal.end + 1,
                              "'{}' {} ".format(match[1], match[2].upper())))
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
