"""Table families: copies of one schema told apart by a table-name prefix.

A warehouse migrated from one system to another often keeps both copies,
``OLD_ROOMS`` beside ``NEW_ROOMS`` and ``OLD_BUILDING`` beside
``NEW_BUILDING``; a regional one keeps ``EU_ORDERS`` beside ``US_ORDERS``.
Each prefix is a *family*, and tables with the same name after it are
*copies* of one another. A join from one family into another's copy --
``OLD_ROOMS`` to ``NEW_BUILDING`` on ``building_id`` -- runs and pairs rows of
two datasets: ids renumbered in the migration match the wrong building, or
none.

Per schema and per pair of prefixes ``P`` / ``Q`` (the idea follows NVIDIA
Auto Ontology's term families):

1. **Copies.** ``P_X`` and ``Q_X`` whose column names, each without its
   table's prefix, overlap by Jaccard >= :data:`MIN_COPY_JACCARD`, or
   :data:`MIN_LINKED_COPY_JACCARD` when one of them is an end of a mirrored
   foreign key. History tables (``*_HIST``, ``*_HISTORY``) are no copies.
2. **Mirrored foreign keys.** A declared foreign key ``P_A.x -> P_B.y`` whose
   counterpart ``Q_A.x -> Q_B.y`` is declared too, names compared without the
   prefixes.
3. **Evidence.** Copies plus mirrored foreign keys between copies must reach
   :data:`MIN_EVIDENCE`: one name two prefixes happen to share is no family.

A confirmed prefix's family is every table of the schema with that prefix.
Prefixes confirmed with one another (``V1_``, ``V2_``, ``V3_``) form one
:class:`FamilyGroup`.

The prefix is a name's first token, split at anything but letters and digits;
a name of one token has none. Names only, from discovery: no database access.
"""

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from itertools import combinations
from typing import Any

#: How alike the columns of two tables of one name must be to be copies.
MIN_COPY_JACCARD = 0.8

#: The same, for a table at either end of a mirrored foreign key.
MIN_LINKED_COPY_JACCARD = 0.5

#: Copies plus mirrored foreign keys that confirm a pair of prefixes.
MIN_EVIDENCE = 3

_HISTORY = frozenset({"HIST", "HISTORY"})
_TOKEN_SPLIT = re.compile(r"[^A-Za-z0-9]+")

# An undirected foreign key between two (name after the prefix, column) ends.
_Signature = frozenset[tuple[str, str]]


def split_name(name: str) -> tuple[str, str] | None:
    """A table name's prefix and the rest, upper-cased.

    Args:
        name: A table name.

    Returns:
        ``("OLD", "ROOMS")`` for ``old_rooms``; None for a name of one token.
    """
    tokens = [t for t in _TOKEN_SPLIT.split(name.upper()) if t]
    if len(tokens) < 2:
        return None
    return tokens[0], "_".join(tokens[1:])


def _without(column: str, prefix: str) -> str:
    """*column* upper-cased, without a leading ``<prefix>_``."""
    upper = column.upper()
    lead = f"{prefix}_"
    return upper[len(lead) :] if upper.startswith(lead) and upper != lead else upper


@dataclass(frozen=True)
class FamilyGroup:
    """Prefixes that mark copies of one schema, and the tables they hold."""

    schema: str
    #: Each prefix, upper-cased, and the names of its tables, sorted.
    families: Mapping[str, tuple[str, ...]]
    #: The names after the prefix of the tables found copied, sorted.
    copied: tuple[str, ...]


@dataclass(frozen=True)
class _Table:
    name: str
    prefix: str
    base: str
    columns: frozenset[str]
    history: bool


def find_families(tables: Iterable[Any]) -> list[FamilyGroup]:
    """The table families of *tables*, per schema.

    Args:
        tables: Discovered tables (``TableInfo``): name, schema, columns and
            declared foreign keys are read.

    Returns:
        One group per set of prefixes confirmed together, by schema and
        prefix.
    """
    by_schema: dict[str, list[Any]] = defaultdict(list)
    for table in tables:
        by_schema[table.schema or ""].append(table)
    groups: list[FamilyGroup] = []
    for schema in sorted(by_schema):
        groups.extend(_schema_families(schema, by_schema[schema]))
    return groups


class FamilyIndex:
    """Which family each table is in, and each family's copy of a name.

    Args:
        members: ``(schema, table name, family prefix)`` for every member, as
            :func:`find_families` found them or an ontology records them. A
            table whose name does not start with its prefix is left out.
    """

    def __init__(self, members: Iterable[tuple[str, str, str]]) -> None:
        self._family: dict[tuple[str, str], tuple[str, str]] = {}
        self._copies: dict[tuple[str, str, str], str] = {}
        for schema, table, family in members:
            split = split_name(table)
            prefix = family.upper()
            if split is None or split[0] != prefix:
                continue
            self._family[(schema.lower(), table.lower())] = (prefix, split[1])
            self._copies[(schema.lower(), prefix, split[1])] = table

    @classmethod
    def of(cls, groups: Iterable[FamilyGroup]) -> "FamilyIndex":
        """The index of the families :func:`find_families` found."""
        return cls(
            (group.schema, name, prefix)
            for group in groups
            for prefix, names in group.families.items()
            for name in names
        )

    def __bool__(self) -> bool:
        return bool(self._family)

    def family(self, schema: str, table: str) -> str | None:
        """The prefix of *table*'s family; None for a table in none."""
        found = self._family.get((schema.lower(), table.lower()))
        return found[0] if found else None

    def own_copies(
        self, schema: str, a: str, b: str
    ) -> tuple[str | None, str | None] | None:
        """For *a* and *b* of two families of *schema*: each family's own copy.

        Args:
            schema: The schema of both tables.
            a: A table.
            b: Another table.

        Returns:
            ``(a's family's copy of b, b's family's copy of a)``, either None
            where that family has none. None when *a* and *b* are not tables
            of two families, or are two copies of one table.
        """
        key = schema.lower()
        family_a = self._family.get((key, a.lower()))
        family_b = self._family.get((key, b.lower()))
        if family_a is None or family_b is None:
            return None
        (prefix_a, base_a), (prefix_b, base_b) = family_a, family_b
        if prefix_a == prefix_b or base_a == base_b:
            return None
        return (
            self._copies.get((key, prefix_a, base_b)),
            self._copies.get((key, prefix_b, base_a)),
        )

    def crosses(self, schema: str, a: str, b: str) -> bool:
        """Whether joining *a* to *b* crosses into another copy of the schema.

        True when they are tables of two families and either family has its
        own copy of the other table: ``OLD_ROOMS`` to ``NEW_BUILDING`` while
        ``OLD_BUILDING`` exists. A table no family has its own copy of is
        shared by all of them.
        """
        copies = self.own_copies(schema, a, b)
        return copies is not None and any(copies)


def _schema_families(schema: str, infos: list[Any]) -> list[FamilyGroup]:
    # prefix -> name after it -> table; the first of a name wins.
    prefixed: dict[str, dict[str, _Table]] = defaultdict(dict)
    members: dict[str, list[str]] = defaultdict(list)
    by_name: dict[str, _Table] = {}
    for info in infos:
        split = split_name(info.name)
        if split is None:
            continue
        prefix, base = split
        table = _Table(
            name=info.name,
            prefix=prefix,
            base=base,
            columns=frozenset(_without(c.name, prefix) for c in info.columns),
            history=base.rsplit("_", 1)[-1] in _HISTORY,
        )
        prefixed[prefix].setdefault(base, table)
        members[prefix].append(info.name)
        by_name[info.name.lower()] = table

    signatures = _signatures(schema, infos, by_name)

    # The names two prefixes share, collected per name so only prefixes that
    # share one are ever paired.
    shared: dict[tuple[str, str], list[str]] = defaultdict(list)
    holders: dict[str, list[str]] = defaultdict(list)
    for prefix in sorted(prefixed):
        for base in prefixed[prefix]:
            holders[base].append(prefix)
    for base, prefixes in holders.items():
        for pair in combinations(prefixes, 2):
            shared[pair].append(base)

    confirmed: list[tuple[str, str, list[str]]] = []
    for (p, q), bases in sorted(shared.items()):
        copies = _confirm(
            [(prefixed[p][b], prefixed[q][b]) for b in sorted(bases)],
            signatures[p] & signatures[q],
        )
        if copies:
            confirmed.append((p, q, copies))

    # Prefixes confirmed with one another are one group.
    parent: dict[str, str] = {}

    def root(prefix: str) -> str:
        parent.setdefault(prefix, prefix)
        while parent[prefix] != prefix:
            parent[prefix] = parent[parent[prefix]]
            prefix = parent[prefix]
        return prefix

    for p, q, _ in confirmed:
        parent[root(q)] = root(p)
    prefixes_of: dict[str, set[str]] = defaultdict(set)
    copied_of: dict[str, set[str]] = defaultdict(set)
    for p, q, copies in confirmed:
        prefixes_of[root(p)].update((p, q))
        copied_of[root(p)].update(copies)
    return [
        FamilyGroup(
            schema=schema,
            families={p: tuple(sorted(members[p])) for p in sorted(prefixes)},
            copied=tuple(sorted(copied_of[top])),
        )
        for top, prefixes in sorted(prefixes_of.items(), key=lambda i: min(i[1]))
    ]


def _signatures(
    schema: str, infos: list[Any], by_name: Mapping[str, _Table]
) -> dict[str, set[_Signature]]:
    """Declared foreign keys between tables of one prefix, without the prefix."""
    signatures: dict[str, set[_Signature]] = defaultdict(set)
    for info in infos:
        source = by_name.get(info.name.lower())
        if source is None:
            continue
        for fk in info.foreign_keys or []:
            if fk.get("referenced_schema") not in (None, "", schema):
                continue
            target = by_name.get(str(fk.get("referenced_table") or "").lower())
            if target is None or target is source or target.prefix != source.prefix:
                continue
            signatures[source.prefix].add(
                frozenset(
                    {
                        (source.base, _without(str(fk.get("column")), source.prefix)),
                        (
                            target.base,
                            _without(str(fk.get("referenced_column")), target.prefix),
                        ),
                    }
                )
            )
    return signatures


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    return len(a & b) / len(a | b) if a | b else 0.0


def _confirm(
    pairs: list[tuple[_Table, _Table]], mirrored: set[_Signature]
) -> list[str]:
    """The copied names of two prefixes, or none when the evidence is short.

    Args:
        pairs: The tables of one name under each prefix.
        mirrored: Foreign keys declared under both prefixes.
    """
    linked = {base for signature in mirrored for base, _ in signature}
    copies = []
    for a, b in pairs:
        if a.history or b.history:
            continue
        bar = MIN_LINKED_COPY_JACCARD if a.base in linked else MIN_COPY_JACCARD
        if _jaccard(a.columns, b.columns) >= bar:
            copies.append(a.base)
    copied = set(copies)
    between_copies = sum(
        1 for signature in mirrored if all(base in copied for base, _ in signature)
    )
    return copies if len(copies) + between_copies >= MIN_EVIDENCE else []


#: What table families are, for a response that lists them.
TABLE_FAMILIES_NOTE = (
    "Copies of one schema told apart by a table-name prefix: a migration's "
    "OLD_ and NEW_ tables, regional EU_ and US_ ones. Join tables of one "
    "family with each other: a join into another family's copy runs and "
    "pairs rows of two datasets."
)
