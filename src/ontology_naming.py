"""Local names of the OWL vocabulary generated from a database schema.

The ontology generator and the R2RML generator both name classes and
properties after tables and columns. They must agree exactly: a mapping that
emits ``ont:orders`` is only useful next to an ontology that declares
``ont:orders``. Both take their names from here.
"""

import re
from dataclasses import dataclass, field
from typing import Any


def clean_name(name: str) -> str:
    """Clean a name to make it suitable for URIs."""
    if not name:
        return "unnamed"

    # Replace spaces and special characters with underscores
    cleaned = re.sub(r"[^a-zA-Z0-9_]", "_", name)

    # Ensure it starts with a letter or underscore
    if cleaned and not (cleaned[0].isalpha() or cleaned[0] == "_"):
        cleaned = "_" + cleaned

    return cleaned or "unnamed"


def class_local_name(table_name: str) -> str:
    """Local name of the OWL class for a table."""
    return clean_name(table_name)


def column_property_local_name(table_name: str, column_name: str) -> str:
    """Local name of the datatype property for a column."""
    return f"{clean_name(table_name)}_{clean_name(column_name)}"


def relationship_local_name(
    table_name: str, referenced_table: str, via_column: str | None = None
) -> str:
    """Local name of the object property for a foreign key.

    ``via_column`` tells apart several keys from one table to another.
    """
    name = f"{clean_name(table_name)}_has_{clean_name(referenced_table)}"
    return f"{name}_via_{clean_name(via_column)}" if via_column else name


def inverse_relationship_local_name(
    table_name: str, referenced_table: str, via_column: str | None = None
) -> str:
    """Local name of the inverse object property for a foreign key."""
    name = f"{clean_name(referenced_table)}_referenced_by_{clean_name(table_name)}"
    return f"{name}_via_{clean_name(via_column)}" if via_column else name


@dataclass
class ForeignKey:
    """One foreign key of a table, which may span several columns."""

    referenced_table: str
    entries: list[dict[str, Any]] = field(default_factory=list)
    via_column: str | None = None
    local_name: str = ""
    inverse_local_name: str = ""

    @property
    def column_pairs(self) -> list[tuple[str, str | None]]:
        """(key column, referenced column) for each column of the key."""
        return [(e["column"], e.get("referenced_column")) for e in self.entries]


def foreign_keys(table_name: str, fk_entries: list[dict[str, Any]]) -> list[ForeignKey]:
    """Group a table's foreign key entries into keys and name them.

    Drivers report one entry per key column, tagged with the constraint it
    belongs to (``constraint_name``); the entries of one constraint are one
    key. An entry without that tag (a schema saved by an older version) is a
    key of its own: which columns form a key cannot be told from their names
    -- two keys to ``users.id`` and ``users.email`` look like one composite.

    A key is named after the two tables. When a table has several keys to the
    same table, each is also named after its first column, so they stay
    separate relationships.

    Args:
        table_name: The referencing table
        fk_entries: Its foreign key entries, as drivers report them

    Returns:
        The keys, in the order their first entry appears
    """
    by_constraint: dict[Any, ForeignKey] = {}
    for i, entry in enumerate(fk_entries):
        if not entry.get("referenced_table") or not entry.get("column"):
            continue
        constraint = entry.get("constraint_name")
        group = (constraint, entry["referenced_table"]) if constraint else i
        if group not in by_constraint:
            by_constraint[group] = ForeignKey(entry["referenced_table"])
        by_constraint[group].entries.append(entry)
    keys = list(by_constraint.values())

    per_target: dict[str, int] = {}
    for key in keys:
        per_target[key.referenced_table] = per_target.get(key.referenced_table, 0) + 1
    for key in keys:
        if per_target[key.referenced_table] > 1:
            key.via_column = key.entries[0]["column"]
        key.local_name = relationship_local_name(
            table_name, key.referenced_table, key.via_column
        )
        key.inverse_local_name = inverse_relationship_local_name(
            table_name, key.referenced_table, key.via_column
        )
    return keys


def assumed_key_column(table_name: str, column_names: list[str]) -> str | None:
    """The column taken as a keyless table's key, judged by its name alone.

    Warehouses rarely declare keys. Relationships to such tables are inferred
    from names (``customer_id`` -> ``customers.id``), which already assumes
    that ``customers.id`` identifies a row; this names that assumption, so
    every consumer makes the same one. It is never verified here.

    Args:
        table_name: The table
        column_names: Its columns

    Returns:
        ``id``, else ``<table>_id`` or ``<table>_sk`` (also singular), or None
    """
    table = table_name.lower()
    stems = [table]
    if table.endswith("ies"):
        stems.append(table[:-3] + "y")
    elif table.endswith("es"):
        stems.append(table[:-2])
    if table.endswith("s"):
        stems.append(table[:-1])
    candidates = ["id"] + [
        f"{stem}{suffix}" for suffix in ("_id", "_sk") for stem in stems
    ]
    by_lower = {c.lower(): c for c in column_names}
    for candidate in candidates:
        if candidate in by_lower:
            return by_lower[candidate]
    return None
