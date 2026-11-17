"""SQL the drivers send on their own behalf, built as sqlglot trees.

Names go into the tree as identifiers and values as literals, and sqlglot
writes them out with each dialect's own quoting and escaping. Nothing here
pastes a name or a value into SQL text, so no name -- a schema called
``a"b``, a dataset with a quote in it -- can change what a statement does.

What is not built here: the user's own query, which a driver wraps for a
syntax check (``EXPLAIN <query>``, ``PREPARE ... AS <query>``). That query is
text by nature; the read-only check in :mod:`src.security` guards it.
"""

from collections.abc import Sequence

from sqlglot import exp


def quoted(name: str) -> exp.Identifier:
    """*name* as a quoted identifier: exact case, any character."""
    return exp.to_identifier(name, quoted=True)


def plain(name: str) -> exp.Identifier:
    """*name* as an unquoted identifier, for fixed catalog names."""
    return exp.to_identifier(name, quoted=False)


def table_path(*parts: exp.Identifier) -> exp.Table:
    """A table reference from identifiers, outermost first, any depth."""
    if len(parts) <= 3:
        names = dict(zip(("this", "db", "catalog"), reversed(parts), strict=False))
        return exp.Table(**names)
    return exp.Table(this=exp.Dot.build(list(parts)))


def quoted_path(*parts: str | None) -> exp.Table:
    """A table reference from name parts, each quoted; empty parts left out."""
    return table_path(*(quoted(part) for part in parts if part))


def qualified_name(dialect: str, *parts: str | None) -> str:
    """Quoted, dotted name parts as SQL, for statements sqlglot cannot build.

    ``SHOW TABLES IN <name>`` and the like: the keywords are fixed text and
    only the name is generated.
    """
    return quoted_path(*parts).sql(dialect=dialect)


def sample_rows(dialect: str, limit: int, *path: str | None) -> str:
    """``SELECT * FROM <path> LIMIT <limit>``, every name part quoted."""
    return (
        exp.select("*", dialect=dialect)
        .from_(quoted_path(*path), dialect=dialect)
        .limit(int(limit), dialect=dialect)
        .sql(dialect=dialect)
    )


def _negated(condition: exp.Expression) -> exp.Expression:
    """``NOT (<condition>)``, bracketed whatever the dialect's NOT precedence."""
    return exp.not_(exp.Paren(this=condition))


def names_except(
    dialect: str,
    column: str,
    source: exp.Table,
    excluded: Sequence[str],
    not_like: Sequence[str] = (),
) -> str:
    """``SELECT <column> FROM <source>`` without *excluded* names, sorted.

    Args:
        dialect: sqlglot dialect.
        column: The name column, e.g. ``schema_name``.
        source: Where the names are listed, e.g. ``information_schema.schemata``.
        excluded: Names to leave out.
        not_like: LIKE patterns to leave out as well.

    Returns:
        The query.
    """
    # Each negated test in brackets: NOT (x IN (...)). Bare NOT x IN (...)
    # reads as (NOT x) IN (...) under MySQL's HIGH_NOT_PRECEDENCE mode.
    conditions = [
        _negated(
            exp.column(column).isin(*(exp.Literal.string(name) for name in excluded))
        ),
        *(
            _negated(exp.column(column).like(exp.Literal.string(pattern)))
            for pattern in not_like
        ),
    ]
    return (
        exp.select(column, dialect=dialect)
        .from_(source, dialect=dialect)
        .where(*conditions, dialect=dialect)
        .order_by(column, dialect=dialect)
        .sql(dialect=dialect)
    )


def select_where(
    dialect: str,
    columns: Sequence[str],
    source: exp.Table,
    equals: dict[str, str] | None = None,
    order_by: str | None = None,
) -> str:
    """``SELECT <columns> FROM <source> WHERE <column> = '<value>' AND ...``.

    Args:
        dialect: sqlglot dialect.
        columns: Columns to select, as fixed names.
        source: The table.
        equals: Column -> value filters; values become string literals.
        order_by: A column to sort by.

    Returns:
        The query.
    """
    query = exp.select(*columns, dialect=dialect).from_(source, dialect=dialect)
    for column, value in (equals or {}).items():
        query = query.where(
            exp.column(column).eq(exp.Literal.string(value)), dialect=dialect
        )
    if order_by:
        query = query.order_by(order_by, dialect=dialect)
    return query.sql(dialect=dialect)
