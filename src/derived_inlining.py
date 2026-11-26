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
  written into both places;
- a ``UNION ALL`` is tried one branch at a time, under the union's column
  names: a total over the union is inflated if any branch repeats the rows it
  sums;
- a derived table joined by inner joins only is moved to ``FROM`` -- sqlglot
  merges from there alone -- the joins becoming conditions in ``WHERE``, the
  comma form OBQC judges like the ``JOIN ... ON`` one.

A derived table on either side of an outer join is left where it is.

No MCP or ontology dependency: the caller passes the tables' columns.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping

from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.merge_subqueries import merge_subqueries
from sqlglot.optimizer.qualify import qualify

logger = logging.getLogger(__name__)

#: Most queries built from one: the query itself, then one per UNION ALL branch.
MAX_VARIANTS = 32

#: {schema: {table: {column: type}}}, as sqlglot's MappingSchema reads it.
Schema = Mapping[str, Mapping[str, Mapping[str, str]]]

# What a rewrite of an unusual shape may raise; it then judges nothing more.
_FAILURES = (SqlglotError, ValueError, KeyError, AttributeError, TypeError)


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
        The query with its relations written in, then one more per UNION ALL
        branch. Empty when the query cannot be rewritten, which only means
        nothing more is judged.
    """
    tree = _qualified(parsed, dialect, schema)
    if tree is None:
        return []
    try:
        _expand_ctes(tree)
        variants = [tree, *_union_branches(tree)][:MAX_VARIANTS]
        merged = []
        for variant in variants:
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

    In order of definition, so a body that reads an earlier CTE is expanded
    before it is copied. A table refers to the CTE a WITH around it defines
    under its name -- the innermost such WITH, and not the CTE whose own body
    the table is in, where the name means a table of the schema.
    """
    for with_ in list(tree.find_all(exp.With)):
        if with_.args.get("recursive") or with_.parent is None:
            continue
        owner = with_.parent
        for cte in list(with_.expressions):
            if not isinstance(cte, exp.CTE):
                continue
            name = cte.alias_or_name.lower()
            for table in list(owner.find_all(exp.Table)):
                if (
                    table.name.lower() != name
                    or table.args.get("db")
                    or table.args.get("catalog")
                    or _definition(table, name) is not cte
                ):
                    continue
                table.replace(
                    exp.Subquery(
                        this=cte.this.copy(),
                        alias=exp.TableAlias(
                            this=exp.to_identifier(table.alias or table.name)
                        ),
                    )
                )
        with_.pop()


def _definition(table: exp.Table, name: str) -> exp.CTE | None:
    """The CTE a reference named *name* reads, or None for a schema table."""
    node: exp.Expr | None = table.parent
    inside: exp.CTE | None = None
    while node is not None:
        if isinstance(node, exp.CTE):
            inside = node
        with_ = _arg(node, "with")
        if isinstance(with_, exp.With):
            for cte in with_.expressions:
                if (
                    isinstance(cte, exp.CTE)
                    and cte.alias_or_name.lower() == name
                    and cte is not inside
                ):
                    return cte
        node = node.parent
    return None


def _union_branches(tree: exp.Expr) -> Iterator[exp.Expr]:
    """*tree* once per branch of each UNION ALL that is read as a relation.

    The branch takes the union's place under the union's column names, which
    are its first branch's. A union at the top of the query is not read by
    anything: each of its branches is a query of its own, judged as such.
    """
    subqueries = list(tree.find_all(exp.Subquery))
    for index, subquery in enumerate(subqueries):
        branches = _branches(subquery.this)
        if len(branches) < 2:
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
            yield variant


def _branches(query: exp.Expr) -> list[exp.Select]:
    """The SELECTs of a UNION ALL chain; empty if anything else is in it."""
    if isinstance(query, exp.Union) and not query.args.get("distinct"):
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

    When every join of *select* is an inner or a cross join, their order does
    not matter: the first derived table that keeps its rows becomes the
    ``FROM``, every other relation a comma join, and the join conditions go
    to ``WHERE``. Nothing happens when ``FROM`` already holds one.
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
            i
            for i, j in enumerate(joins)
            if isinstance(j.this, exp.Subquery) and _keeps_rows(j.this.this)
        ),
        None,
    )
    if position is None:
        return
    conditions = [j.args["on"] for j in joins if j.args.get("on") is not None]
    others = [from_.this, *(j.this for i, j in enumerate(joins) if i != position)]
    select.set(_key(select, "from"), exp.From(this=joins[position].this.copy()))
    select.set("joins", [exp.Join(this=relation.copy()) for relation in others])
    for condition in conditions:
        select.where(condition.copy(), copy=False)
