"""Conservative filter lineage for SQLGlot query scopes.

This module resolves a *filter*, rather than merely proving a column's lineage.
Returning a base column does not assert that a physical partition was scanned.
"""

from collections import Counter

from sqlglot import exp
from sqlglot.optimizer.scope import Scope


def _is_star(expression):
    return isinstance(expression, exp.Star) or (
        isinstance(expression, exp.Column) and isinstance(expression.this, exp.Star)
    )


def _inside(node, ancestor):
    while node is not None:
        if node is ancestor:
            return True
        node = node.parent
    return False


class PartitionResolver:
    """Resolve safe local filters to ``(Table, Column, derived)`` tuples.

    ``detmap`` is keyed by ``id(Table)`` as in analyze.py. ``reason`` describes
    conservative exclusions, including partial UNION branches, from the latest
    resolution of that scope/column. The caller should retain these diagnostics
    even when another UNION branch was resolved successfully.
    """

    def __init__(self, scopes, detmap):
        self.scopes = list(scopes)
        self.detmap = detmap
        self.scope_by_node = {id(scope.expression): scope for scope in self.scopes}
        self.reference_counts = Counter(
            id(source)
            for scope in self.scopes
            for _, source in scope.selected_sources.values()
            if isinstance(source, Scope)
        )
        self._reasons = {}

    @staticmethod
    def _key(scope, column):
        return (id(scope), column.catalog, column.db, column.table, column.name)

    @staticmethod
    def _column(column):
        return exp.Column(this=column.this.copy())

    def resolve(self, scope, column):
        reasons = []
        result = self._local(scope, column, set(), reasons)
        self._reasons[self._key(scope, column)] = "；".join(dict.fromkeys(reasons))
        # The same CTE/UNION lineage can reach an occurrence more than once.
        unique = {}
        for table, mapped, derived in result:
            unique[(id(table), mapped.name)] = (table, mapped, derived)
        return list(unique.values())

    def reason(self, scope, column):
        return self._reasons.get(self._key(scope, column), "")

    def _local(self, scope, column, seen, reasons):
        if scope is None:
            reasons.append("未找到查询作用域")
            return []
        key = ("local",) + self._key(scope, column)
        if key in seen:
            reasons.append("递归字段血缘，未归因到底表")
            return []
        seen = seen | {key}
        sources = {name: item[1] for name, item in scope.selected_sources.items()}
        if column.table:
            source = sources.get(column.table)
            if source is None:
                reasons.append("字段不属于当前作用域；相关子查询未向父作用域分配条件")
                return []
        elif len(sources) == 1:
            source = next(iter(sources.values()))
        else:
            reasons.append("多源或无源作用域中的未限定字段，归属不明确")
            return []
        if isinstance(source, exp.Table):
            if column.db and (
                source.db != column.db or source.name != column.table
                or (column.catalog and source.catalog != column.catalog)
            ):
                reasons.append("字段的全限定表名与当前来源不一致")
                return []
            if source.args.get("sample"):
                reasons.append("TABLESAMPLE屏障，仅有字段血缘，未下推筛选")
                return []
            return [(source, self._column(column), False)] if id(source) in self.detmap else []
        if isinstance(source, Scope):
            if column.db or column.catalog:
                reasons.append("派生来源不能按schema/catalog限定字段")
                return []
            return self._output(source, column, seen, reasons)
        reasons.append("来源不是可解析的底表或查询")
        return []

    def _barrier(self, scope):
        query = scope.expression
        if self.reference_counts[id(scope)] > 1:
            return "共享CTE/派生来源有多个消费者，未将单一消费者筛选下推"
        cte = query.parent if isinstance(query.parent, exp.CTE) else None
        if cte is not None:
            if cte.args.get("materialized"):
                return "MATERIALIZED CTE屏障，未下推筛选"
            if isinstance(cte.parent, exp.With) and cte.parent.args.get("recursive"):
                return "递归CTE屏障，未下推筛选"
        if query.args.get("limit") is not None or query.args.get("offset") is not None:
            return "LIMIT/OFFSET屏障，仅有字段血缘，未下推筛选"
        if isinstance(query, exp.Select):
            distinct = query.args.get("distinct")
            if distinct is not None and distinct.args.get("on") is not None:
                return "DISTINCT ON屏障，未下推筛选"
            if any(window.find_ancestor(exp.Select) is query for window in query.find_all(exp.Window)):
                return "窗口计算屏障，未下推筛选"
            if query.args.get("qualify") is not None:
                return "QUALIFY屏障，未下推筛选"
        return ""

    def _projection_scope(self, scope):
        """The leftmost SELECT defines a set operation's output names."""
        while isinstance(scope.expression, exp.SetOperation):
            scope = self._branch_scope(scope.expression.this)
            if scope is None:
                return None
        return scope

    def _branch_scope(self, branch):
        # Parenthesized SELECT/UNION operands do not have separate scopes in
        # SQLGlot 26; their underlying query does.
        while isinstance(branch, (exp.Subquery, exp.Paren)):
            branch = branch.this
        return self.scope_by_node.get(id(branch))

    def _output(self, scope, column, seen, reasons):
        barrier = self._barrier(scope)
        if barrier:
            reasons.append(barrier)
            return []
        projection_scope = self._projection_scope(scope)
        if projection_scope is None or not isinstance(projection_scope.expression, exp.Select):
            reasons.append("派生来源输出投影未展开")
            return []
        projections = projection_scope.expression.expressions
        outer_columns = scope.outer_columns
        names = [outer_columns[i] if i < len(outer_columns) else p.alias_or_name
                 for i, p in enumerate(projections)]
        matches = [i for i, name in enumerate(names) if name == column.name and not _is_star(projections[i])]
        if len(matches) == 1:
            index = matches[0]
            if any(_is_star(p) for p in projections[:index]):
                reasons.append("星号投影之前的输出列数量未知，无法按位置追溯")
                return []
            return self._position(scope, index, seen, reasons)
        if len(matches) > 1:
            reasons.append("派生来源输出字段重名，归属不明确")
            return []
        # A single-source SELECT * can pass a named field through without a
        # schema. A UNION star requires ordinal expansion and stays unknown.
        stars = [p for p in projections if _is_star(p)]
        if (isinstance(scope.expression, exp.Select) and len(stars) == 1
                and len(projections) == 1 and not outer_columns):
            star = stars[0]
            mapped = self._column(column)
            if isinstance(star, exp.Column):
                mapped.set("table", star.args.get("table").copy() if star.args.get("table") else None)
            return [(t, c, True) for t, c, _ in self._local(scope, mapped, seen, reasons)]
        reasons.append("输出字段未找到，或星号投影缺少列元数据，未归因到底表")
        return []

    def _position(self, scope, index, seen, reasons):
        key = ("position", id(scope), index)
        if key in seen:
            reasons.append("递归输出投影，未归因到底表")
            return []
        seen = seen | {key}
        barrier = self._barrier(scope)
        if barrier:
            reasons.append(barrier)
            return []
        query = scope.expression
        if isinstance(query, exp.SetOperation):
            result = []
            for branch in (query.this, query.expression):
                branch_scope = self._branch_scope(branch)
                if branch_scope is None:
                    reasons.append("集合运算分支作用域未展开")
                    continue
                result.extend(self._position(branch_scope, index, seen, reasons))
            return result
        if not isinstance(query, exp.Select) or index >= len(query.expressions):
            reasons.append("输出投影位置未知")
            return []
        if any(_is_star(p) for p in query.expressions[:index + 1]):
            reasons.append("集合运算星号投影缺少列元数据，未按位置追溯")
            return []
        projection = query.expressions[index]
        projection = projection.this if isinstance(projection, exp.Alias) else projection
        if not isinstance(projection, exp.Column):
            reasons.append("输出字段来自计算列，仅保留血缘，未作为底表分区筛选")
            return []
        return [(t, c, True) for t, c, _ in self._local(scope, projection, seen, reasons)]

    def _source_tables(self, source, seen=None):
        seen = set() if seen is None else seen
        if isinstance(source, exp.Table):
            return {id(source)}
        if not isinstance(source, Scope) or id(source) in seen:
            return set()
        seen = seen | {id(source)}
        if isinstance(source.expression, exp.SetOperation):
            sources = [self._branch_scope(branch) for branch in
                       (source.expression.this, source.expression.expression)]
        else:
            sources = [item[1] for item in source.selected_sources.values()]
        result = set()
        for item in sources:
            result.update(self._source_tables(item, seen))
        return result

    def safe_on(self, join, scope, table):
        return not self.on_reason(join, scope, table)

    def on_reason(self, join, scope, table):
        """Reject ON predicates on a preserved side of an outer/anti join."""
        side = join.side.upper()
        if join.kind.upper() == "ANTI" and not side:
            side = "LEFT"
        if side not in {"LEFT", "RIGHT", "FULL"}:
            return ""
        if scope is None:
            return "外连接作用域未知，ON条件未作为确定分区筛选"
        right_tables = set()
        left_tables = set()
        for node, source in scope.selected_sources.values():
            target = right_tables if _inside(node, join.this) else left_tables
            target.update(self._source_tables(source))
        target_id = id(table)
        safe = ((side == "LEFT" and target_id in right_tables and target_id not in left_tables)
                or (side == "RIGHT" and target_id in left_tables and target_id not in right_tables))
        if safe:
            return ""
        return "外连接保留侧ON条件只限制匹配，未作为底表确定分区筛选"
