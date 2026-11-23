"""Which tables' columns reach a query's result.

``SELECT x.e FROM (SELECT lower(email) AS e FROM users) x`` returns
``users.email``: each output is followed through derived tables, CTEs (also
recursive ones, to a fixed point), set operations (by position, or ``BY
NAME``), column alias lists (``c(e)``, ``AS d(e)``), stars with their
``EXCLUDE``, ``REPLACE`` and ``RENAME``, and scalar subqueries back to the
columns of real tables its value is made of. A column that only decides a value
does not reach the result: one counted (``COUNT(DISTINCT email)``), compared
(``email IS NULL``, ``EXISTS (...)``), tested in a ``CASE``, or used to
partition or filter.

Where a star leaves a column's place open -- a table whose column order the
catalog does not know, a ``USING`` join, a pattern (``* LIKE 'e%'``,
``COLUMNS(...)``) -- names still hold, and whatever reads by position (a set
operation, an alias list) takes every column the star could have put there.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from sqlglot import exp
from sqlglot.optimizer.scope import Scope, build_scope

logger = logging.getLogger(__name__)

#: A real table's column, as the caller's catalog names them.
Column = tuple[str, str]


@dataclass(frozen=True)
class Known:
    """A table's columns, as a catalog knows them."""

    columns: tuple[Column, ...]
    #: Whether *columns* are in the database's order, as ``SELECT *`` returns
    #: them.
    ordered: bool


#: The columns of a table a query reads; None for a table not known.
Catalog = Callable[[exp.Table], Known | None]

# Rounds a recursive CTE gets to settle. Each round can only add columns, so
# it settles well before; past this, every output takes every column.
_MAX_ROUNDS = 100

_Item = tuple[str, frozenset[Column]]
_Source = exp.Expr | Scope


@dataclass(frozen=True)
class _Outputs:
    """A query's outputs, in order: (lower-cased name, the columns it holds)."""

    items: tuple[_Item, ...] = ()
    #: Whether each output is at its place: False once a star left that open.
    ordered: bool = True

    def everything(self) -> frozenset[Column]:
        return frozenset(c for _, found in self.items for c in found)

    def named(self, name: str) -> frozenset[Column] | None:
        hits = [found for output, found in self.items if output == name]
        return frozenset(c for found in hits for c in found) if hits else None

    def names(self) -> list[str]:
        return [name for name, _ in self.items]


def _spread(names: Iterable[str], every: frozenset[Column]) -> _Outputs:
    """Outputs whose places are unknown: each may hold any of *every*."""
    items = tuple((name, every) for name in dict.fromkeys(names))
    return _Outputs(items or (("", every),), ordered=False)


def _renamed(outputs: _Outputs, alias: exp.Expr | None) -> _Outputs:
    """*outputs* under an alias's column list: ``c(e)``, ``AS d(e)``."""
    columns = alias.args.get("columns") if isinstance(alias, exp.TableAlias) else None
    names = [column.name.lower() for column in columns or []]
    if not names:
        return outputs
    if outputs.ordered and len(outputs.items) >= len(names):
        return _Outputs(
            tuple(
                (names[i] if i < len(names) else name, found)
                for i, (name, found) in enumerate(outputs.items)
            )
        )
    # Which output each name is given is open: each may be any.
    every = outputs.everything()
    return _Outputs(tuple((n, every) for n in names) + outputs.items, ordered=False)


def _grown(before: _Outputs, after: _Outputs) -> _Outputs:
    """*after*, holding at least what *before* did: a fixed point only grows."""
    if not before.items:
        return after
    if before.names() == after.names() and before.ordered == after.ordered:
        return _Outputs(
            tuple(
                (name, old | new)
                for (name, old), (_, new) in zip(before.items, after.items, strict=True)
            ),
            after.ordered,
        )
    return _spread(
        before.names() + after.names(), before.everything() | after.everything()
    )


def _selected(scope: Scope) -> list[tuple[exp.Expr, _Source]]:
    """*scope*'s FROM and JOIN sources, in order, each with its node."""
    return [(node, source) for node, source in scope.selected_sources.values()]


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
    return set(_Lineage(root, catalog).outputs(root).everything())


class _Lineage:
    """Each scope's outputs, with the real columns each is made of."""

    def __init__(self, root: Scope, catalog: Catalog) -> None:
        self._catalog = catalog
        self._scopes = {id(scope.expression): scope for scope in root.traverse()}
        self._done: dict[int, _Outputs] = {}
        # Recursive CTEs being worked out, with what is known of them so far.
        self._partial: dict[int, _Outputs] = {}
        self._reread: set[int] = set()
        # Projections being followed: SELECT e AS e names itself.
        self._following: set[int] = set()
        # Each table read, as the catalog knows it.
        self._tables: dict[int, _Outputs] = {}

    def outputs(self, scope: Scope) -> _Outputs:
        """*scope*'s outputs, under its CTE's column list if it has one."""
        key = id(scope.expression)
        if key in self._done:
            return self._done[key]
        if key in self._partial:
            # A recursive CTE reading itself: what it holds so far.
            self._reread.add(key)
            return self._partial[key]
        self._partial[key] = self._seed(scope)
        result = self._partial[key]
        for _ in range(_MAX_ROUNDS):
            self._reread.discard(key)
            known = set(self._done)
            result = _grown(
                self._partial[key], self._declared(scope, self._compute(scope))
            )
            if key not in self._reread or result == self._partial[key]:
                break
            # Another round: what was worked out from the last guess is redone.
            self._partial[key] = result
            for stale in set(self._done) - known:
                del self._done[stale]
        else:
            logger.debug("Recursive CTE lineage did not settle; widened")
            result = _spread(result.names(), result.everything())
        del self._partial[key]
        self._done[key] = result
        return result

    def _seed(self, scope: Scope) -> _Outputs:
        """What a recursive CTE holds before it reads itself: its first branch."""
        if (
            isinstance(scope.expression, exp.SetOperation)
            and scope.set_operation_scopes
        ):
            return self._declared(scope, self.outputs(scope.set_operation_scopes[0]))
        return _Outputs()

    @staticmethod
    def _declared(scope: Scope, outputs: _Outputs) -> _Outputs:
        parent = scope.expression.parent
        if isinstance(parent, exp.CTE):
            return _renamed(outputs, parent.args.get("alias"))
        return outputs

    def _compute(self, scope: Scope) -> _Outputs:
        node = scope.expression
        if isinstance(node, exp.SetOperation):
            return self._set_operation(scope, node)
        if isinstance(node, exp.Select):
            items: list[_Item] = []
            ordered = True
            for projection in node.selects:
                part = self._projection(projection, scope)
                items.extend(part.items)
                ordered = ordered and part.ordered
            return _Outputs(tuple(items), ordered)
        return _Outputs()

    def _set_operation(self, scope: Scope, node: exp.SetOperation) -> _Outputs:
        branches = [self.outputs(branch) for branch in scope.set_operation_scopes]
        if isinstance(node, exp.Except):
            # Rows of the first branch only.
            branches = branches[:1]
        if not branches:
            return _Outputs()
        if node.args.get("by_name"):
            names = dict.fromkeys(name for b in branches for name in b.names())
            return _Outputs(
                tuple(
                    (name, frozenset(c for b in branches for c in b.named(name) or ()))
                    for name in names
                ),
                all(b.ordered for b in branches),
            )
        first = branches[0]
        if all(b.ordered and len(b.items) == len(first.items) for b in branches):
            return _Outputs(
                tuple(
                    (name, frozenset(c for b in branches for c in b.items[i][1]))
                    for i, (name, _) in enumerate(first.items)
                )
            )
        # By position, with a place left open: every output may be any.
        return _spread(
            [name for b in branches for name in b.names()],
            frozenset(c for b in branches for c in b.everything()),
        )

    def _projection(self, projection: exp.Expr, scope: Scope) -> _Outputs:
        star, table, pattern = self._star_of(projection)
        if star is not None:
            sources = self._named(scope, table) if table else _selected(scope)
            merged = not table and self._merges_columns(scope)
            return self._star(star, sources, scope, open_places=merged or pattern)
        if any(True for _ in projection.find_all(exp.Columns)):
            # COLUMNS(...): as many outputs as columns match, named after them.
            every = frozenset(self._value(projection, scope))
            sources = _selected(scope)
            names = [n for node, s in sources for n in self._source(node, s).names()]
            return _spread(names, every)
        name = projection.alias_or_name.lower()
        return _Outputs(((name, frozenset(self._value(projection, scope))),))

    @staticmethod
    def _star_of(projection: exp.Expr) -> tuple[exp.Star | None, str, bool]:
        """The star *projection* is, the table it is of, and if a pattern picks.

        ``*``, ``c.*``, ``* ILIKE '%mail%'`` and ``* LIKE 'e%'`` (which sqlglot
        reads as a comparison of the star) all return columns.
        """
        node = projection.this if isinstance(projection, exp.Not) else projection
        pattern = node is not projection
        if isinstance(node, exp.Predicate) and isinstance(
            node.this, (exp.Star, exp.Column)
        ):
            node, pattern = node.this, True
        if isinstance(node, exp.Star):
            return node, "", pattern or node.args.get("ilike") is not None
        if isinstance(node, exp.Column) and isinstance(node.this, exp.Star):
            star = node.this
            return star, node.table, pattern or star.args.get("ilike") is not None
        return None, "", False

    def _star(
        self,
        star: exp.Star,
        sources: list[tuple[exp.Expr, _Source]],
        scope: Scope,
        open_places: bool,
    ) -> _Outputs:
        excluded = {
            column.name.lower()
            for key in ("except_", "except")
            for column in star.args.get(key) or []
        }
        # REPLACE (lower(email) AS email): the output keeps its name and place
        # and holds the replacement's value.
        replaced = {
            alias.alias.lower(): frozenset(self._value(alias.this, scope))
            for alias in star.args.get("replace") or []
            if isinstance(alias, exp.Alias)
        }
        renamed = {
            alias.this.name.lower(): alias.alias.lower()
            for alias in star.args.get("rename") or []
            if isinstance(alias, exp.Alias)
        }
        parts = [self._source(node, source) for node, source in sources]
        ordered = not open_places and all(part.ordered for part in parts)
        items: list[_Item] = []
        seen = set()
        for part in parts:
            for name, found in part.items:
                if name in excluded:
                    continue
                seen.add(name)
                items.append((renamed.get(name, name), replaced.get(name, found)))
        for name, found in replaced.items():
            # A column the catalog lacks: its replacement is still returned.
            if name not in seen and name not in excluded:
                items.append((name, found))
                ordered = False
        return _Outputs(tuple(items), ordered)

    @staticmethod
    def _merges_columns(scope: Scope) -> bool:
        """Whether a ``USING`` or ``NATURAL`` join folds columns in ``*``."""
        return any(
            join.args.get("using")
            or (join.args.get("method") or "").upper() == "NATURAL"
            for join in scope.expression.args.get("joins") or []
        )

    def _value(self, expr: exp.Expr, scope: Scope) -> set[Column]:
        """The real columns *expr*'s value is made of."""
        if isinstance(expr, exp.Column):
            if isinstance(expr.this, exp.Star):
                return self._everything(self._named(scope, expr.table))
            return self._column(expr, scope)
        if isinstance(expr, exp.Columns):
            return self._everything(_selected(scope))
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
            subquery = self._scopes.get(id(query))
            return set(self.outputs(subquery).everything()) if subquery else set()
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
        qualifier = column.table
        current: Scope | None = scope
        while current is not None:
            if qualifier:
                sources = self._named(current, qualifier)
                if sources:
                    return {
                        c
                        for node, source in sources
                        for c in self._source(node, source).named(name) or ()
                    }
            else:
                found: set[Column] | None = None
                for node, source in _selected(current):
                    hit = self._source(node, source).named(name)
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
    def _named(scope: Scope, name: str) -> list[tuple[exp.Expr, _Source]]:
        """The FROM or JOIN sources of *scope* called *name*."""
        return [
            pair
            for key, pair in scope.selected_sources.items()
            if key.lower() == name.lower()
        ]

    def _source(self, node: exp.Expr, source: _Source) -> _Outputs:
        """What a FROM or JOIN source returns, under its alias's column list."""
        if isinstance(source, Scope):
            outputs = self.outputs(self._recursive_cte(source) or source)
        elif isinstance(source, exp.Table):
            outputs = self._table(source)
        else:
            return _Outputs(ordered=False)
        alias = node.args.get("alias")
        if alias is None and isinstance(source, Scope):
            # A derived table's node is its SELECT; the alias is the subquery's.
            parent = source.expression.parent
            if isinstance(parent, exp.Subquery):
                alias = parent.args.get("alias")
        return _renamed(outputs, alias)

    def _table(self, table: exp.Table) -> _Outputs:
        key = id(table)
        if key not in self._tables:
            known = self._catalog(table)
            self._tables[key] = (
                _Outputs(
                    tuple((c.lower(), frozenset({(t, c)})) for t, c in known.columns),
                    known.ordered,
                )
                if known is not None
                else _Outputs(ordered=False)
            )
        return self._tables[key]

    def _recursive_cte(self, source: Scope) -> Scope | None:
        """The whole CTE, when *source* is a recursive CTE reading itself.

        sqlglot resolves that reference to the CTE's first branch alone, so
        what later rounds add, and the CTE's column list, would be missed.
        """
        top = None
        parent = source.expression.parent
        while isinstance(parent, exp.SetOperation):
            top, parent = parent, parent.parent
        if top is not None and isinstance(parent, exp.CTE):
            return self._scopes.get(id(top))
        return None

    def _everything(self, sources: list[tuple[exp.Expr, _Source]]) -> set[Column]:
        return {
            column
            for node, source in sources
            for column in self._source(node, source).everything()
        }
