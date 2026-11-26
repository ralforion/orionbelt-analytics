"""Sketches of the query a fan-trap finding asks for, to adapt rather than run.

When OBQC blocks an aggregate for a fan-out, the model has to write the query
again. A sentence ("pre-aggregate, or UNION ALL") leaves the shape to it; a
sketch written with the tables and the join columns of the finding shows it.

A sketch is deliberately not a query. Its placeholders -- ``<group keys>``,
``<order_items value>`` -- are not SQL, so one sent back unchanged fails to
parse instead of running half filled in, and the model still decides what the
question means: which rows to keep, which aggregates to take. Writing the
finished query would take that decision from it, and the SQL alone often does
not settle it: ``sales JOIN returns`` may mean "sales and returns" or "sales
that were returned".

Two shapes, chosen by what the finding shows:

- **One leg per fact** (``UNION ALL``), when the query aggregates both tables
  of the fan-out -- sales and their returns, or two facts sharing a parent.
  Each fact is read on its own, so neither repeats the other's rows.
- **A semi-join** (``EXISTS``), when the table that fans out is only joined to
  filter -- "orders that contain a book". Each row of the measured table is
  read once, and kept when a matching row exists.

No MCP or ontology dependency: the validator passes table and column names.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .name_suggestions import sql_name

#: ``shape`` of a sketch whose facts are read one leg each, combined by UNION ALL.
SHAPE_LEGS = "union_all_legs"
#: ``shape`` of a sketch that reads one table and keeps rows by EXISTS.
SHAPE_SEMI_JOIN = "semi_join"

_INDENT = "    "


@dataclass(frozen=True)
class Link:
    """A many-to-one join from *child* to *parent*, by the key's columns."""

    child: str
    parent: str
    #: (child column, parent column) for every column of the key.
    pairs: tuple[tuple[str, str], ...]

    def condition(self, dialect: str) -> str:
        """``returns.order_item_id = order_items.id``, as *dialect* writes it."""
        child, parent = sql_name(self.child, dialect), sql_name(self.parent, dialect)
        return " AND ".join(
            f"{child}.{sql_name(c, dialect)} = {parent}.{sql_name(p, dialect)}"
            for c, p in self.pairs
        )


def _value_alias(table: str, dialect: str) -> str:
    """The leg column carrying *table*'s values: ``order_items_value``."""
    return sql_name(f"{table}_value", dialect, exact_case=False)


def union_all_legs(
    tables: Sequence[str],
    links: Mapping[str, Link],
    dialect: str,
    grouped: bool = True,
) -> str:
    """One leg per fact, NULL-padded and combined with UNION ALL.

    Args:
        tables: The facts, one leg each, in the order to write them.
        links: Per fact, the join its leg starts with on its way to the group
            keys, when the finding names one: a return reaches the product
            through its sale. Other legs get only the placeholder.
        dialect: sqlglot dialect the names are written for.
        grouped: Whether the query groups; without it the legs carry values
            only and the outer query aggregates them all.

    Returns:
        The sketch, with ``<...>`` placeholders for what the finding cannot
        know.
    """
    # Inside the legs the keys are columns of the tables, named; outside,
    # only those names are in scope.
    keys = "<group keys AS names>, " if grouped else ""
    aliases = [_value_alias(t, dialect) for t in tables]
    legs: list[str] = []
    for index, table in enumerate(tables):
        # The first leg names the columns; the others line up by position.
        values = [
            (
                (f"<{table} value>" if i == index else "NULL")
                + (f" AS {alias}" if index == 0 else "")
            )
            for i, alias in enumerate(aliases)
        ]
        lines = [
            f"SELECT {keys}{', '.join(values)}",
            f"FROM {sql_name(table, dialect)}",
        ]
        link = links.get(table)
        if link is not None:
            lines.append(
                f"JOIN {sql_name(link.parent, dialect)} ON {link.condition(dialect)}"
            )
        if grouped:
            lines.append("<many-to-one joins to the group keys>")
        lines.append("<the query's filters that apply to this fact>")
        legs.append("\n".join(_INDENT + line for line in lines))
    outer_keys = "<group key names>, " if grouped else ""
    aggregates = ", ".join(f"SUM({alias})" for alias in aliases)
    body = f"\n{_INDENT}UNION ALL\n".join(legs)
    sketch = [
        "-- Sketch, not a query: fill in each <...>. One leg per fact, so neither",
        "-- repeats the other's rows; keep the query's own aggregates (SUM, AVG,",
        "-- COUNT all skip the NULL padding).",
        "WITH legs AS (",
        body,
        ")",
        f"SELECT {outer_keys}{aggregates}",
        "FROM legs",
    ]
    if grouped:
        sketch.append("GROUP BY <group key names>")
    return "\n".join(sketch)


def semi_join(measure: str, link: Link, dialect: str, grouped: bool = True) -> str:
    """*measure* read once per row, kept when a matching *link.child* row exists.

    Args:
        measure: The table whose values are aggregated.
        link: The join from the table that fans out (its child) to *measure*.
        dialect: sqlglot dialect the names are written for.
        grouped: Whether the query groups.

    Returns:
        The sketch, with ``<...>`` placeholders.
    """
    table = sql_name(measure, dialect)
    keys = "<group keys>, " if grouped else ""
    sketch = [
        f"-- Sketch, not a query: fill in each <...>. Each {measure} row is read",
        f"-- once, and kept when a matching {link.child} row exists.",
        f"SELECT {keys}<the query's aggregates of {measure}>",
        f"FROM {table}",
    ]
    if grouped:
        sketch.append("<many-to-one joins to the group keys>")
    sketch += [
        "WHERE EXISTS (",
        f"{_INDENT}SELECT 1",
        f"{_INDENT}FROM {sql_name(link.child, dialect)}",
        f"{_INDENT}<its joins and filters, as in the query>",
        f"{_INDENT}WHERE {link.condition(dialect)}",
        ")",
        "<AND the query's other filters>",
    ]
    if grouped:
        sketch.append("GROUP BY <group keys>")
    return "\n".join(sketch)


def only_with(measure: str, link: Link, dialect: str) -> str:
    """The line a legs sketch adds for "only rows that have a match".

    Text for a model to read, never sent to a database; built from its
    clauses like :func:`semi_join`'s lines.
    """
    exists = " ".join(
        [
            "WHERE EXISTS (SELECT 1",
            f"FROM {sql_name(link.child, dialect)}",
            f"WHERE {link.condition(dialect)})",
        ]
    )
    return (
        f"-- Only the {measure} rows that have a {link.child} row? Add to the "
        f"{measure} leg:\n--   {exists}"
    )
