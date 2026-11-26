"""A query with its CTEs and derived tables written into the queries that read them.

OBQC judges each SELECT's joins against that SELECT's aggregates. A join inside
a CTE that aggregates nothing is safe there, and the outer SELECT that sums its
rows joins nothing -- so neither saw the fan-out between them:

    WITH x AS (SELECT o.total FROM orders o JOIN order_items oi ON ...)
    SELECT SUM(total) FROM x

returns every order's total once per item, and passed. Written as one SELECT,
``SELECT SUM(o.total) FROM orders o JOIN order_items oi ON ...``, the same
query is an ordinary fan-trap. This module writes those single SELECTs, for
the fan-trap rules to judge as well; it never changes what runs.

A relation is written in only where that keeps its rows as they are. A SELECT
that groups, aggregates, de-duplicates, numbers its rows by a window or limits
them changes which rows reach its reader -- ``ROW_NUMBER() ... = 1`` is the
usual way to undo a fan-out -- so it stays as written, and is judged on its
own as before. sqlglot's ``merge_subqueries`` makes that call; this module
brings the relations to where it can:

- every reference to a CTE becomes a copy of its body, so a CTE read twice is
  written into both places -- each name bound as SQL binds it: a CTE sees
  only the CTEs declared before it, an inner WITH hides an outer one;
- a ``UNION ALL`` is tried one branch at a time, under the union's column
  names, a union read by a union with both replaced: a total over the union
  is inflated if any branch repeats the rows it sums;
- a derived table joined by inner joins only is moved to ``FROM`` -- sqlglot
  merges from there alone -- and each join condition kept in the ``ON`` of
  the first join that has every relation it reads, since the columns a join
  compares say which relationship it uses.

A derived table on either side of an outer join is left where it is, and so
is a union that limits its rows (``LIMIT``, ``OFFSET``, ``FETCH``). Copying
CTE bodies has a budget (:data:`MAX_GROWTH`, :data:`MAX_NODES`): a chain of
CTEs each reading its predecessor twice doubles at every stage, and past the
budget the query is judged as written.

No MCP or ontology dependency: the caller passes the tables' columns.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from itertools import islice

from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.merge_subqueries import merge_subqueries
from sqlglot.optimizer.qualify import qualify

logger = logging.getLogger(__name__)

#: Most queries built from one: one per choice of UNION ALL branches.
MAX_VARIANTS = 32

#: {schema: {table: {column: type}}}, as sqlglot's MappingSchema reads it.
Schema = Mapping[str, Mapping[str, Mapping[str, str]]]

#: How much larger than the query its CTEs written in may grow: each reference
#: is a copy of a body, and a CTE that reads its predecessor twice doubles the
#: query at every stage of a chain.
MAX_GROWTH = 10
#: The most nodes the written-in query may have, however large the query.
MAX_NODES = 50_000


class _Abandon(Exception):
    """The rewrite cannot be done faithfully or cheaply; judge as written."""


# What a rewrite of an unusual shape may raise; it then judges nothing more.
_FAILURES = (
    _Abandon,
    SqlglotError,
    ValueError,
    KeyError,
    AttributeError,
    TypeError,
)


def _arg(node: exp.Expr, name: str) -> exp.Expr | None:
    """*node*'s ``from`` or ``with``, under the name this sqlglot gives it."""
    value = node.args.get(f"{name}_")
    return value if value is not None else node.args.get(name)


def _key(node: exp.Expr, name: str) -> str:
    return f"{name}_" if f"{name}_" in type(node).arg_types else name


def worth_inlining(parsed: exp.Expr) -> bool:
    """Whether *parsed* aggregates and reads a CTE or a derived table."""
    if next(parsed.find_all(exp.AggFunc), None) is None:
        return False
    return next(parsed.find_all(exp.CTE), None) is not None or any(
        isinstance(node.this, exp.Query) for node in parsed.find_all(exp.Subquery)
    )


def inlined(
    parsed: exp.Expr, dialect: str, schema: Schema | None = None
) -> list[exp.Expr]:
    """*parsed* with every relation that keeps its rows written into its reader.

    Args:
        parsed: The query, as sqlglot parsed it. Not modified.
        dialect: sqlglot dialect it is written in.
        schema: The tables' columns, so that unqualified names and stars
            inside a relation resolve; without it, only what the query
            qualifies itself can be followed.

    Returns:
        The query with its relations written in: once, or -- when it reads
        a UNION ALL -- once per choice of branches, at most
        :data:`MAX_VARIANTS`. Empty when the query cannot be rewritten, which
        only means nothing more is judged.
    """
    tree = _qualified(parsed, dialect, schema)
    if tree is None:
        return []
    try:
        _expand_ctes(tree)
        merged = []
        # Taken one at a time: each variant is a copy of the whole query.
        for variant in islice(_union_variants(tree), MAX_VARIANTS):
            for select in list(variant.find_all(exp.Select)):
                _derived_to_from(select)
            merged.append(merge_subqueries(variant))
        return merged
    except _FAILURES as error:
        logger.debug("Not writing CTEs and derived tables in: %s", error)
        return []


def _qualified(
    parsed: exp.Expr, dialect: str, schema: Schema | None
) -> exp.Expr | None:
    """*parsed* with every column qualified by its relation, or None."""
    for attempt in (schema, None) if schema else (None,):
        try:
            qualified: exp.Expr = qualify(
                parsed.copy(),
                dialect=dialect,
                schema=dict(attempt) if attempt else None,
                validate_qualify_columns=False,
                quote_identifiers=False,
                identify=False,
            )
            return qualified
        except _FAILURES as error:
            logger.debug("Could not qualify the query: %s", error)
    return None


def _expand_ctes(tree: exp.Expr) -> None:
    """Every reference to a non-recursive CTE becomes a copy of its body.

    Scoped as SQL scopes the names: a WITH's CTEs are expanded in the order
    they are declared, each seeing only those before it -- in ``WITH x AS
    (SELECT total FROM orders), orders AS (...)``, ``x`` reads the table --
    and the query the WITH belongs to sees all of them, nested subqueries
    and nested WITHs included, an inner name hiding an outer one. A body is
    expanded before it is copied, so no copy is ever resolved again out of
    its place. A recursive WITH is left as it is, and its names hide outer
    ones of the same name.

    A reference with a column alias list (``FROM x AS y(t)``) renames the
    body's outputs, by position, to its columns.

    Raises:
        _Abandon: When the copies would make the query more than
            :data:`MAX_GROWTH` times larger (or :data:`MAX_NODES` nodes), or a
            column list cannot be put on the body.
    """
    size = _nodes(tree)
    budget = _Budget(min(MAX_GROWTH * size + 1_000, MAX_NODES) - size)
    _expand(tree, {}, budget)


class _Budget:
    """Nodes the copies of CTE bodies may still add."""

    def __init__(self, left: int) -> None:
        self.left = left
        self._sizes: dict[int, int] = {}

    def spend(self, body: exp.Expr) -> None:
        size = self._sizes.get(id(body))
        if size is None:
            size = self._sizes[id(body)] = _nodes(body)
        self.left -= size
        if self.left < 0:
            raise _Abandon("CTE expansion over budget")


def _nodes(node: exp.Expr) -> int:
    return sum(1 for _ in node.walk())


def _rename_outputs(query: exp.Expr, names: list[str]) -> None:
    """Give *query*'s first outputs *names*, as a column alias list does.

    A union's outputs are named by its first branch, so that is renamed.

    Raises:
        _Abandon: When the outputs cannot be named so: a star among them, or
            fewer outputs than names.
    """
    while isinstance(query, exp.Union):
        query = query.left
    if not isinstance(query, exp.Select):
        raise _Abandon("column list on a query that is no SELECT")
    selects = query.selects
    if len(names) > len(selects) or any(
        isinstance(p, exp.Star) or isinstance(p.this, exp.Star) for p in selects
    ):
        raise _Abandon("column list the outputs cannot take")
    query.set(
        "expressions",
        [
            (
                exp.alias_(p.this if isinstance(p, exp.Alias) else p, names[i])
                if i < len(names)
                else p
            )
            for i, p in enumerate(selects)
        ],
    )


def _expand(node: exp.Expr, env: dict[str, exp.Expr | None], budget: _Budget) -> None:
    """Expand the CTE references under *node*; *env* maps a name to its body.

    A name mapped to None is a recursive CTE's: in sight, never written in.
    """
    with_ = _arg(node, "with")
    if isinstance(with_, exp.With):
        env = dict(env)
        ctes = [c for c in with_.expressions if isinstance(c, exp.CTE)]
        if with_.args.get("recursive"):
            for cte in ctes:
                env[cte.alias_or_name.lower()] = None
            for cte in ctes:
                _expand(cte.this, env, budget)
        else:
            for cte in ctes:
                _expand(cte.this, env, budget)
                env[cte.alias_or_name.lower()] = cte.this
            with_.pop()
    if (
        isinstance(node, exp.Table)
        and node.name
        and not node.args.get("db")
        and not node.args.get("catalog")
    ):
        body = env.get(node.name.lower())
        if body is not None:
            budget.spend(body)
            copied = body.copy()
            alias = node.args.get("alias")
            columns = alias.columns if isinstance(alias, exp.TableAlias) else []
            if columns:
                _rename_outputs(copied, [c.name for c in columns])
            node.replace(
                exp.Subquery(
                    this=copied,
                    alias=exp.TableAlias(
                        this=exp.to_identifier(node.alias or node.name)
                    ),
                )
            )
        return
    for child in list(node.iter_expressions()):
        _expand(child, env, budget)


def _union_variants(tree: exp.Expr) -> Iterator[exp.Expr]:
    """*tree* once per choice of a branch for each UNION ALL read as a relation.

    The outermost union first: its chosen branch takes its place, under the
    union's column names (its first branch's), and the unions that branch
    reads are chosen for in turn -- so a union that reads another through a
    CTE has both boundaries removed. Depth first and lazy, so a caller
    taking a few variants pays for a few. A query reading no union is
    yielded as it is.

    A union at the top of the query is read by nothing: each of its branches
    is a query of its own, judged as such. Nor is one that limits its rows
    (``LIMIT``, ``OFFSET``, ``FETCH``) replaced by a branch, which would drop
    the limit.
    """
    for index, subquery in enumerate(tree.find_all(exp.Subquery)):
        branches = _branches(subquery.this)
        if len(branches) < 2 or _limits(subquery):
            continue
        names = [projection.alias_or_name for projection in branches[0].selects]
        if any(len(branch.selects) != len(names) for branch in branches):
            continue
        for branch in branches:
            variant = tree.copy()
            target = list(variant.find_all(exp.Subquery))[index]
            renamed = branch.copy()
            renamed.set(
                "expressions",
                [
                    exp.alias_(p.this if isinstance(p, exp.Alias) else p, name)
                    for p, name in zip(renamed.selects, names, strict=True)
                ],
            )
            target.set("this", renamed)
            yield from _union_variants(variant)
        return
    yield tree


def _limits(node: exp.Expr) -> bool:
    """Whether *node* caps or skips rows: ``LIMIT``, ``OFFSET``, ``FETCH``."""
    return any(node.args.get(arg) for arg in ("limit", "offset", "fetch"))


def _branches(query: exp.Expr) -> list[exp.Select]:
    """The SELECTs of a UNION ALL chain; empty if anything else is in it.

    Empty too when a union in the chain limits its rows: no branch alone
    stands for what reaches its reader then.
    """
    if isinstance(query, exp.Union) and not query.args.get("distinct"):
        if _limits(query):
            return []
        left, right = _branches(query.left), _branches(query.right)
        return [*left, *right] if left and right else []
    return [query] if isinstance(query, exp.Select) else []


def _keeps_rows(query: exp.Expr) -> bool:
    """Whether *query* passes on every row its joins produce, once each."""
    if not isinstance(query, exp.Select):
        return False
    return not (
        query.args.get("group")
        or query.args.get("distinct")
        or query.args.get("limit")
        or query.args.get("having")
        or query.args.get("qualify")
        or next(query.find_all(exp.AggFunc, exp.Window), None) is not None
    )


def _derived_to_from(select: exp.Select) -> None:
    """Move a derived table joined by inner joins only to ``FROM``.

    sqlglot writes a derived table into its reader from ``FROM`` alone. When
    every join of *select* is an inner or a cross join, their order does not
    matter: the first derived table that keeps its rows becomes the ``FROM``
    and the other relations follow in their order. Each condition of the
    joins goes to the ``ON`` of the first join after which every relation it
    reads is there -- kept in an ``ON``, not moved to ``WHERE``, since which
    columns a join compares is what says which relationship it uses.

    Nothing happens when ``FROM`` already holds one, or when a condition
    reads something other than the relations of *select* by their names.
    """
    joins = select.args.get("joins") or []
    from_ = _arg(select, "from")
    if not joins or from_ is None:
        return
    if isinstance(from_.this, exp.Subquery) and _keeps_rows(from_.this.this):
        return
    if any(
        j.side
        or (j.kind or "").upper() not in ("", "INNER", "CROSS")
        or j.args.get("using")
        for j in joins
    ):
        return
    position = next(
        (
            i + 1
            for i, j in enumerate(joins)
            if isinstance(j.this, exp.Subquery) and _keeps_rows(j.this.this)
        ),
        None,
    )
    if position is None:
        return
    relations = [from_.this, *(j.this for j in joins)]
    names = [r.alias_or_name.lower() for r in relations]
    if not all(names) or len(set(names)) != len(names):
        return
    conjuncts: list[exp.Expr] = []
    for join in joins:
        on = join.args.get("on")
        if on is not None:
            on = on.unnest()
            conjuncts += list(on.flatten()) if isinstance(on, exp.And) else [on]
    reads: list[set[str]] = []
    for conjunct in conjuncts:
        columns = list(conjunct.find_all(exp.Column))
        read = {c.table.lower() for c in columns}
        if (
            any(not c.table for c in columns)
            or not read <= set(names)
            or next(conjunct.find_all(exp.Query), None) is not None
        ):
            return
        reads.append(read)
    order = [position, *(i for i in range(len(relations)) if i != position)]
    there = {names[position]}
    pending = list(range(len(conjuncts)))
    rejoined = []
    for i in order[1:]:
        there.add(names[i])
        mine = [k for k in pending if reads[k] <= there]
        pending = [k for k in pending if k not in mine]
        join = exp.Join(this=relations[i].copy())
        if mine:
            join.set("on", exp.and_(*(conjuncts[k].copy() for k in mine)))
        rejoined.append(join)
    select.set(_key(select, "from"), exp.From(this=relations[position].copy()))
    select.set("joins", rejoined)
