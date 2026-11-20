"""Which tables' columns reach a query's result.

``SELECT x.e FROM (SELECT lower(email) AS e FROM users) x`` returns
``users.email``: each output is followed through derived tables, CTEs, set
operations, stars and scalar subqueries back to the columns of real tables its
value is made of. A column that only decides a value does not reach the
result: one counted (``COUNT(DISTINCT email)``), compared (``email IS NULL``,
``EXISTS (...)``), tested in a ``CASE``, or used to partition or filter.
"""

from __future__ import annotations

from collections.abc import Callable

from sqlglot import exp
from sqlglot.optimizer.scope import Scope, build_scope

#: A real table's column, as the caller's catalog names them.
Column = tuple[str, str]
#: The columns of a table a query reads, in order; None for one not known.
Catalog = Callable[[exp.Table], list[Column] | None]

_Output = tuple[str, frozenset[Column]]


def result_columns(parsed: exp.Expr, catalog: Catalog) -> set[Column]:
    """The real tables' columns whose values *parsed* returns.

    Args:
        parsed: The parsed query.
        catalog: The columns of each table it reads.

    Returns:
        (table, column) pairs, as *catalog* names them.
    """
    root = build_scope(parsed)
    if root is None:
        return set()
    lineage = _Lineage(catalog)
    return {column for _, columns in lineage.outputs(root) for column in columns}


class _Lineage:
    """Each scope's outputs, by name, with the real columns each is made of."""

    def __init__(self, catalog: Catalog) -> None:
        self._catalog = catalog
        self._outputs: dict[int, list[_Output]] = {}
        # Projections being followed: SELECT e AS e names itself.
        self._following: set[int] = set()

    def outputs(self, scope: Scope) -> list[_Output]:
        """*scope*'s outputs in order, each as (lower-cased name, its columns)."""
        key = id(scope)
        if key in self._outputs:
            return self._outputs[key]
        # A recursive CTE reads itself: what it reads of itself adds nothing.
        self._outputs[key] = []
        node = scope.expression
        outputs: list[_Output] = []
        if isinstance(node, exp.SetOperation):
            branches = [self.outputs(branch) for branch in scope.set_operation_scopes]
            if branches and all(len(b) == len(branches[0]) for b in branches):
                # Named by the first branch, made of each branch's at its place.
                for position, (name, _) in enumerate(branches[0]):
                    merged = {c for b in branches for c in b[position][1]}
                    outputs.append((name, frozenset(merged)))
            else:
                # A star over a table the catalog lacks hides the positions:
                # every name may hold any branch's columns.
                every = frozenset(c for b in branches for _, found in b for c in found)
                names = dict.fromkeys(name for b in branches for name, _ in b)
                outputs = [(name, every) for name in names] or [("", every)]
        elif isinstance(node, exp.Select):
            for projection in node.selects:
                outputs.extend(self._projection(projection, scope))
        self._outputs[key] = outputs
        return outputs

    def _projection(self, projection: exp.Expr, scope: Scope) -> list[_Output]:
        sources: list[exp.Expr | Scope]
        if isinstance(projection, exp.Star):
            star = projection
            sources = [source for _, source in scope.selected_sources.values()]
        elif isinstance(projection, exp.Column) and isinstance(
            projection.this, exp.Star
        ):
            star = projection.this
            sources = self._named(scope, projection.table)
        else:
            name = projection.alias_or_name.lower()
            return [(name, frozenset(self._value(projection, scope)))]
        excluded = {
            column.name.lower()
            for key in ("except_", "except")
            for column in star.args.get(key) or []
        }
        return [
            output
            for source in sources
            for output in self._source_outputs(source)
            if output[0] not in excluded
        ]

    def _value(self, expr: exp.Expr, scope: Scope) -> set[Column]:
        """The real columns *expr*'s value is made of."""
        if isinstance(expr, exp.Column):
            if isinstance(expr.this, exp.Star):
                return self._everything(self._named(scope, expr.table))
            return self._column(expr, scope)
        # A count, a comparison or a test answers about values, returning none.
        if isinstance(
            expr, (exp.Count, exp.CountIf, exp.ApproxDistinct, exp.Predicate)
        ):
            return set()
        # A CASE or IF returns its branches; its conditions only choose one.
        if isinstance(expr, exp.Case):
            branches = [b.args.get("true") for b in expr.args.get("ifs") or []]
            return self._values([*branches, expr.args.get("default")], scope)
        if isinstance(expr, exp.If):
            return self._values([expr.args.get("true"), expr.args.get("false")], scope)
        # PARTITION BY and ORDER BY of a window, FILTER of an aggregate: what
        # rows a value is taken over, not the value.
        if isinstance(expr, (exp.Window, exp.Filter)):
            return self._value(expr.this, scope)
        if isinstance(expr, (exp.Subquery, exp.Query)):
            query = expr.this if isinstance(expr, exp.Subquery) else expr
            for subquery in scope.subquery_scopes:
                if subquery.expression is query:
                    return {c for _, found in self.outputs(subquery) for c in found}
            return set()
        return self._values(list(expr.iter_expressions()), scope)

    def _values(self, exprs: list[exp.Expr | None], scope: Scope) -> set[Column]:
        """The real columns the values of *exprs* are made of."""
        found: set[Column] = set()
        for expr in exprs:
            if expr is not None:
                found |= self._value(expr, scope)
        return found

    def _column(self, column: exp.Column, scope: Scope) -> set[Column]:
        """What *column* reads, in *scope* or, correlated, an enclosing one."""
        name = column.name.lower()
        qualifier = column.table.lower()
        current: Scope | None = scope
        while current is not None:
            if qualifier:
                for key, source in current.sources.items():
                    if key.lower() == qualifier:
                        return self._source_column(source, name) or set()
            else:
                found: set[Column] | None = None
                for _, source in current.selected_sources.values():
                    hit = self._source_column(source, name)
                    if hit is not None:
                        found = (found or set()) | hit
                if found is not None:
                    return found
                # A whole row: SELECT to_json(u) FROM users u.
                rows = self._named(current, name)
                if rows:
                    return self._everything(rows)
                # An earlier output of the same SELECT (DuckDB, Snowflake).
                earlier = self._output_named(current, name, column)
                if earlier is not None:
                    return earlier
            current = current.parent if current.is_subquery else None
        return set()

    def _output_named(
        self, scope: Scope, name: str, column: exp.Column
    ) -> set[Column] | None:
        node = scope.expression
        if not isinstance(node, exp.Select):
            return None
        for projection in node.selects:
            if (
                isinstance(projection, exp.Alias)
                and projection.alias.lower() == name
                and id(projection) not in self._following
                and column.find_ancestor(exp.Alias) is not projection
            ):
                self._following.add(id(projection))
                try:
                    return self._value(projection.this, scope)
                finally:
                    self._following.discard(id(projection))
        return None

    @staticmethod
    def _named(scope: Scope, name: str) -> list[exp.Expr | Scope]:
        """The FROM or JOIN source of *scope* called *name*."""
        return [
            source
            for key, (_, source) in scope.selected_sources.items()
            if key.lower() == name.lower()
        ]

    def _source_outputs(self, source: exp.Expr | Scope) -> list[_Output]:
        if isinstance(source, Scope):
            return self.outputs(source)
        if isinstance(source, exp.Table):
            return [
                (column.lower(), frozenset({(table, column)}))
                for table, column in self._catalog(source) or []
            ]
        return []

    def _everything(self, sources: list[exp.Expr | Scope]) -> set[Column]:
        return {
            column
            for source in sources
            for _, found in self._source_outputs(source)
            for column in found
        }

    def _source_column(self, source: exp.Expr | Scope, name: str) -> set[Column] | None:
        """What *source*'s output *name* is made of; None if it has none."""
        hits = [
            found for output, found in self._source_outputs(source) if output == name
        ]
        if not hits:
            return None
        return {column for found in hits for column in found}
