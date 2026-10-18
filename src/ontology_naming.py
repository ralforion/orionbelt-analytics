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

    Drivers report one entry per key column and no constraint name, so a
    composite key arrives as several entries naming the same table. They are
    taken as one key when each references a different column; when they
    repeat a referenced column, or leave it out, each entry is a key of its
    own (two keys to one table, such as billing and shipping address).

    A key is named after the two tables. When a table has several keys to the
    same table, each is also named after its first column, so they stay
    separate relationships.

    Args:
        table_name: The referencing table
        fk_entries: Its foreign key entries, as drivers report them

    Returns:
        The keys, in the order their first entry appears
    """
    by_target: dict[tuple[str, Any], list[dict[str, Any]]] = {}
    for entry in fk_entries:
        if not entry.get("referenced_table") or not entry.get("column"):
            continue
        target = (entry["referenced_table"], entry.get("referenced_schema"))
        by_target.setdefault(target, []).append(entry)

    keys: list[ForeignKey] = []
    for (ref_table, _), entries in by_target.items():
        ref_columns = [e.get("referenced_column") for e in entries]
        if all(ref_columns) and len(set(ref_columns)) == len(ref_columns):
            keys.append(ForeignKey(ref_table, entries))
        else:
            keys.extend(ForeignKey(ref_table, [e]) for e in entries)

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
