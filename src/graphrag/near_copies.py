"""Near-copies: tables with nearly the same columns.

Warehouses keep several tables of one shape: a current table and its archive,
yearly copies (``orders_2023``, ``orders_2024``), a backup, the same feed
loaded into staging and into production. A search returns whichever scores
highest, and a query on the wrong copy runs and returns plausible rows that are
not the answer. Naming each table's near-copies, with the columns only one side
has, lets the model check which copy the question means before it commits.

Tables are compared by their *content columns*: column names, without case,
leaving out bookkeeping columns (:func:`is_bookkeeping`) -- generic ids, audit
and load stamps, versions, tenancy -- which unrelated tables share as much as
copies do. Two tables are near-copies when

- they share at least :data:`MIN_SHARED` content columns, and
- they share half of all their content columns (Jaccard >= 0.5), or four
  fifths of the narrower table's while the wider has at most twice as many:
  a lookup table whose few columns were copied into a wide fact table is not
  a copy of it.

A table with fewer than three content columns is nobody's near-copy: its
shape says too little to tell a copy from a table that merely looks alike.

Names only, from discovery: no database access and no MCP here.

Comparing every pair is quadratic -- about a second at 2,000 tables, six at
5,000 -- so :class:`CopyIndex` uses a prefix filter, the standard exact
shortcut for set-similarity joins. Both rules above imply that a qualifying
pair shares at least ``max(3, ceil(0.4 * n))`` of the content columns of each
side, *n* being that side's count. With columns ordered rarest first, two sets
sharing at least *o* columns must share one among the first ``n - o + 1`` of
each, so only tables meeting on one of their rarer columns are compared, and
none that qualify are missed.

Tables with the same content columns are compared once, as a group: a
date-sharded export (``events_20240101``, ``events_20240102``, ...) is
thousands of tables of one shape, with no rarer column to tell them apart and
millions of pairs. The rule is decided per pair of groups, pairs are counted
from the group sizes, and only the closest are built.
"""

import heapq
import re
from collections import Counter, defaultdict
from collections.abc import Container, Iterable, Iterator, Mapping
from dataclasses import dataclass
from itertools import islice
from operator import itemgetter
from typing import Any

#: Shared content columns below this never make two tables copies.
MIN_SHARED = 3

#: Near-copies named per table, closest first.
PER_TABLE = 3

#: Own columns listed per side; the rest are counted.
MAX_LISTED_COLUMNS = 8

# Columns that say how a row is kept rather than what it holds. Matched on the
# whole lowercased name, so customer_id, order_date and archived_at stay
# content.
_BOOKKEEPING = re.compile(
    r"""
    _.*                                         # _loaded_at, _fivetran_synced
    | id | uuid | guid | rowid | row_id | oid
    | version | row_version | rowversion | etag
    | tenant | tenant_id
    | is_deleted | deleted
    | (?:date_|last_)?
      (?:created|updated|modified|changed|deleted|inserted|loaded|synced
         |ingested|extracted)
      (?:_(?:at|on|by|date|dt|time|ts|timestamp|utc|user))?
    | (?:create|update|modify|change|insert|creation|modification)
      _(?:at|on|by|date|dt|time|ts|timestamp)
    | (?:etl|dw|dwh|audit)_.*                   # etl_batch_id, dw_insert_ts
    | record_source | load_date | load_ts | load_time | batch_id
    """,
    re.VERBOSE,
)


def is_bookkeeping(column: str) -> bool:
    """Whether *column* records how a row is kept, not what it holds.

    ``id``, ``created_at``, ``updated_by``, ``last_modified``, ``version``,
    ``tenant_id``, ``_loaded_at``, ``etl_batch_id`` and the like. Unrelated
    tables share them, so they are no evidence that two tables are copies.

    Args:
        column: A column name.

    Returns:
        True for a bookkeeping column.
    """
    return bool(_BOOKKEEPING.fullmatch(column.lower()))


def column_names(table: Mapping[str, Any]) -> list[str]:
    """The column names of a table as GraphRAG indexes it.

    Args:
        table: Table metadata whose ``columns`` are dicts or names.

    Returns:
        The names, in column order.
    """
    return [
        str(c.get("name") if isinstance(c, dict) else c)
        for c in table.get("columns") or []
    ]


@dataclass(frozen=True)
class NearCopy:
    """A table that shares most of its content columns with another one."""

    #: The other table's identity (``schema.table``).
    table: str
    #: Columns both tables have, bookkeeping columns included.
    shared: int
    #: Columns only the first table has, in its column order.
    only_here: tuple[str, ...]
    #: Columns only :attr:`table` has, in its column order.
    only_there: tuple[str, ...]
    #: Jaccard similarity of the content columns.
    jaccard: float


def _min_shared(width: int) -> int:
    """The fewest content columns a near-copy shares with a table this wide.

    ``max(3, ceil(0.4 * width))``, in integers. Jaccard >= 0.5 needs half of
    the union, so at least half of either side; containment needs four
    fifths of the narrower side, which is at least half the wider one, so at
    least two fifths of either.
    """
    return max(MIN_SHARED, (2 * width + 4) // 5)


def _similar(shared: int, width: int, other_width: int) -> bool:
    """The rule, on counts of content columns."""
    if shared < MIN_SHARED:
        return False
    union = width + other_width - shared
    narrow, wide = sorted((width, other_width))
    jaccard = 2 * shared >= union
    contained = 5 * shared >= 4 * narrow and wide <= 2 * narrow
    return jaccard or contained


@dataclass(frozen=True)
class NearCopyPairs:
    """The closest near-copy pairs of some tables, and how many there are."""

    #: ``(table, near_copy)``, closest first.
    closest: list[tuple[str, NearCopy]]
    #: Every pair, the ones not in :attr:`closest` included.
    total: int


def _pair_count(size: int, asked: int, other_size: int | None, other_asked: int) -> int:
    """Pairs between two groups, or within one, with a table asked about.

    Args:
        size: Tables in the first group.
        asked: How many of them were asked about.
        other_size: Tables in the second group; None for pairs within the first.
        other_asked: How many of those were asked about.
    """
    if other_size is None:
        return (size * (size - 1) - (size - asked) * (size - asked - 1)) // 2
    return size * other_size - (size - asked) * (other_size - other_asked)


class CopyIndex:
    """The tables of a catalog, indexed to find each one's near-copies.

    Built once per catalog; :meth:`of` then compares a table only with the
    tables that share one of its rarer content columns, and with those that
    have the same content columns without comparing them again.

    Closest first means: the higher Jaccard similarity of the content
    columns, then more content columns shared, then by name.

    Args:
        tables: Each table's identity and its column names.
    """

    def __init__(self, tables: Mapping[str, Iterable[str]]) -> None:
        # Lowercased name -> name as written, in column order.
        self._columns: dict[str, dict[str, str]] = {}
        for identity, names in tables.items():
            columns: dict[str, str] = {}
            for name in names:
                columns.setdefault(str(name).lower(), str(name))
            self._columns[identity] = columns
        # What the rule compares, the content columns, once per group of tables
        # that have the same ones.
        groups: dict[frozenset[str], list[str]] = defaultdict(list)
        for identity, columns in self._columns.items():
            content = frozenset(c for c in columns if not is_bookkeeping(c))
            groups[content].append(identity)
        self._content: list[frozenset[str]] = list(groups)
        self._members: list[list[str]] = [sorted(m) for m in groups.values()]
        self._group = {
            identity: g
            for g, members in enumerate(self._members)
            for identity in members
        }
        frequency = Counter(c for content in self._content for c in content)
        self._prefix: dict[int, list[str]] = {}
        self._postings: dict[str, list[int]] = defaultdict(list)
        for g, content in enumerate(self._content):
            width = len(content)
            needed = _min_shared(width)
            if needed > width:
                continue  # too few content columns to be anyone's near-copy
            rarest_first = sorted(content, key=lambda c: (frequency[c], c))
            self._prefix[g] = rarest_first[: width - needed + 1]
            for column in self._prefix[g]:
                self._postings[column].append(g)

    def __contains__(self, identity: object) -> bool:
        return identity in self._columns

    def _matching(
        self, g: int, skip: Container[int] = ()
    ) -> Iterator[tuple[int, int, float]]:
        """The groups whose tables are near-copies of group *g*'s.

        Group *g* itself first, when its tables could be anyone's near-copy:
        its tables are one another's.

        Args:
            g: A group.
            skip: Groups not to compare with *g*.

        Yields:
            ``(group, shared content columns, Jaccard similarity)``.
        """
        prefix = self._prefix.get(g)
        if prefix is None:
            return
        mine = self._content[g]
        width = len(mine)
        yield g, width, 1.0
        candidates = {h for column in prefix for h in self._postings[column]}
        candidates.discard(g)
        for h in candidates:
            if h in skip:
                continue
            theirs = self._content[h]
            other_width = len(theirs)
            # Both rules need the wider at most twice the narrower.
            if width > 2 * other_width or other_width > 2 * width:
                continue
            shared = len(mine & theirs)
            if _similar(shared, width, other_width):
                yield h, shared, shared / (width + other_width - shared)

    def of(self, identity: str, limit: int | None = PER_TABLE) -> list[NearCopy]:
        """The near-copies of *identity*, closest first.

        Args:
            identity: A table of this index.
            limit: How many to return at most; None for all of them.

        Returns:
            The near-copies; none for a table not in the index.
        """
        g = self._group.get(identity)
        if g is None:
            return []
        found: list[tuple[tuple[float, int, str], float]] = []
        for h, shared, jaccard in self._matching(g):
            others = (t for t in self._members[h] if t != identity)
            # A group's tables tie but for their names, sorted already.
            found.extend(
                ((-jaccard, -shared, other), jaccard) for other in islice(others, limit)
            )
            if limit is not None and len(found) > 2 * limit:
                found = heapq.nsmallest(limit, found, key=itemgetter(0))
        found.sort(key=itemgetter(0))
        return [
            self._near_copy(identity, key[2], jaccard) for key, jaccard in found[:limit]
        ]

    def _near_copy(self, identity: str, other: str, jaccard: float) -> NearCopy:
        mine, theirs = self._columns[identity], self._columns[other]
        return NearCopy(
            table=other,
            shared=len(mine.keys() & theirs.keys()),
            only_here=tuple(n for k, n in mine.items() if k not in theirs),
            only_there=tuple(n for k, n in theirs.items() if k not in mine),
            jaccard=jaccard,
        )

    def pairs(
        self, identities: Iterable[str], limit: int | None = None
    ) -> NearCopyPairs:
        """The near-copy pairs with a table in *identities*: the closest, and a count.

        Every pair is counted, but at most *limit* are kept: thousands of
        tables of one shape are millions of pairs.

        A pair is ``(table, near_copy)``, listed once: *table* is the one in
        *identities*, or the first by name when both are.

        Args:
            identities: The tables whose near-copies are wanted.
            limit: How many pairs to keep at most; None for all of them.

        Returns:
            The closest pairs and how many there are.
        """
        wanted = {i for i in identities if i in self._group}
        asked: dict[int, list[str]] = defaultdict(list)
        for identity in sorted(wanted):
            asked[self._group[identity]].append(identity)
        total = 0
        kept: list[tuple[tuple[float, int, str, str], float]] = []
        # A pair of groups both asked about is taken from the first one.
        done: set[int] = set()
        for g in sorted(asked):
            for h, shared, jaccard in self._matching(g, skip=done):
                total += _pair_count(
                    len(self._members[g]),
                    len(asked[g]),
                    None if h == g else len(self._members[h]),
                    len(asked.get(h, ())),
                )
                found = self._member_pairs(g, h, asked, wanted)
                kept.extend(
                    ((-jaccard, -shared, a, b), jaccard)
                    for a, b in islice(found, limit)
                )
                if limit is not None and len(kept) > 2 * limit:
                    kept = heapq.nsmallest(limit, kept, key=itemgetter(0))
            done.add(g)
        kept.sort(key=itemgetter(0))
        closest = [
            (key[2], self._near_copy(key[2], key[3], jaccard))
            for key, jaccard in kept[:limit]
        ]
        return NearCopyPairs(closest=closest, total=total)

    def _member_pairs(
        self, g: int, h: int, asked: Mapping[int, list[str]], wanted: set[str]
    ) -> Iterator[tuple[str, str]]:
        """The pairs of tables between groups *g* and *h*, or within *g*, by name.

        Lazily, so taking the first few costs no more than those: every table
        of the other group pairs with the first table asked about.
        """
        if g == h:
            members = self._members[g]
            for a in asked[g]:
                for b in members:
                    if b != a and (b not in wanted or b > a):
                        yield a, b
            return
        for a in heapq.merge(asked[g], asked.get(h, [])):
            partners = self._members[h] if self._group[a] == g else self._members[g]
            for b in partners:
                if b not in wanted or b > a:
                    yield a, b


def listed(columns: tuple[str, ...]) -> list[str]:
    """*columns* for a response: the first few, then how many more there are."""
    if len(columns) <= MAX_LISTED_COLUMNS:
        return list(columns)
    more = len(columns) - MAX_LISTED_COLUMNS
    return [*columns[:MAX_LISTED_COLUMNS], f"... and {more} more"]


#: What near-copies are, for a response that lists them.
NEAR_COPIES_NOTE = (
    "Tables with nearly the same columns: a current table and its archive, "
    "yearly or backup copies, the same data loaded twice. Check which one the "
    "question means before querying; a query on the wrong copy runs and "
    "returns plausible rows that are not the answer."
)
