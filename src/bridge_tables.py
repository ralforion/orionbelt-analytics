"""Bridge tables: tables that only link others, many to many.

``student_courses(student_id, course_id)`` holds nothing of its own: it says
which students take which courses. Joining through it repeats each student
once per course and each course once per student, so a total over either side
is right per key of the other side and inflated without it. A table is a
bridge when it has at least two foreign keys and every column belongs to one
of them, apart from a surrogate key and bookkeeping columns (``id``,
``created_at``, ``etl_batch_id``). A self-referential one counts:
``also_bought(product_id, also_product_id)`` links products to products.
``enrollments(student_id, course_id, grade)`` holds a grade of its own and is
no bridge. The idea follows NVIDIA Auto Ontology's bridge tables, which ask a
model to write the join; here the join is the foreign keys.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .bookkeeping import is_bookkeeping
from .ontology_naming import foreign_keys

#: Bridges a discover_schema response lists; the rest are counted.
MAX_LISTED_BRIDGES = 25

BRIDGE_TABLES_NOTE = (
    "Tables that only link others, many to many. A join through one repeats "
    "each row of either side once per row it is linked to: a total over one "
    "side is right per key of the other (GROUP BY courses.id for a sum over "
    "students), and inflated without it."
)


@dataclass(frozen=True)
class Bridge:
    """A table that links others, many to many."""

    schema: str
    table: str
    #: The tables it links, in the order of its keys, each once.
    connects: tuple[str, ...]
    #: Whether its primary key is made of its keys' columns, so each
    #: combination of linked rows appears at most once.
    pairs_unique: bool
    #: Whether a key it rests on was inferred from names, not declared.
    inferred: bool = False


def find_bridges(
    tables: Iterable[Any], inferred: Iterable[tuple[str, str, str]] = ()
) -> list[Bridge]:
    """The bridge tables among *tables*.

    Args:
        tables: Analyzed tables (``TableInfo``).
        inferred: (table, column, referenced table) of foreign keys inferred
            from column names, beside the declared ones.

    Returns:
        The bridges, in table order.
    """
    guessed: dict[str, list[tuple[str, str]]] = {}
    for table, column, target in inferred:
        guessed.setdefault(table.lower(), []).append((column, target))
    found = []
    for table in tables:
        bridge = _bridge(table, guessed.get(table.name.lower(), []))
        if bridge is not None:
            found.append(bridge)
    return found


def _bridge(table: Any, guessed: list[tuple[str, str]]) -> Bridge | None:
    keys: list[tuple[frozenset[str], str, bool]] = [
        (
            frozenset(entry["column"].lower() for entry in key.entries),
            key.referenced_table,
            False,
        )
        for key in foreign_keys(table.name, table.foreign_keys or [])
    ]
    in_keys = {column for columns, _, _ in keys for column in columns}
    for column, target in guessed:
        if column.lower() not in in_keys:
            keys.append((frozenset({column.lower()}), target, True))
            in_keys.add(column.lower())
    if len(keys) < 2:
        return None
    primary = [k.lower() for k in table.primary_keys or []]
    surrogate = (
        set(primary) if len(primary) == 1 and primary[0] not in in_keys else set()
    )
    for column in table.columns:
        name = column.name.lower()
        if name not in in_keys and name not in surrogate and not is_bookkeeping(name):
            return None
    spanned = [columns for columns, _, _ in keys if columns & set(primary)]
    pairs_unique = bool(primary) and set(primary) <= in_keys and len(spanned) >= 2
    return Bridge(
        schema=table.schema or "",
        table=table.name,
        connects=tuple(dict.fromkeys(target for _, target, _ in keys)),
        pairs_unique=pairs_unique,
        inferred=any(guess for _, _, guess in keys),
    )


def listed_bridges(bridges: list[Bridge]) -> dict[str, Any]:
    """*bridges* as discover_schema reports them; empty if there are none."""
    if not bridges:
        return {}
    section: dict[str, Any] = {
        "bridge_tables": [
            {
                "table": bridge.table,
                "connects": list(bridge.connects),
                "pairs_unique": bridge.pairs_unique,
            }
            for bridge in bridges[:MAX_LISTED_BRIDGES]
        ],
        "bridge_tables_note": BRIDGE_TABLES_NOTE,
    }
    if len(bridges) > MAX_LISTED_BRIDGES:
        section["more_bridge_tables"] = len(bridges) - MAX_LISTED_BRIDGES
    return section
