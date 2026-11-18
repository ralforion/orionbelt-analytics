"""Near-copies: tables with nearly the same columns.

Warehouses keep several tables of one shape: a current table and its archive,
yearly copies (``orders_2023``, ``orders_2024``), a backup, the same feed
loaded into staging and into production. A search returns whichever scores
highest, and a query on the wrong copy runs and returns plausible rows that are
not the answer. Naming each table's near-copies, with the columns only one side
has, lets the model check which copy the question means before it commits.

Two tables are near-copies when, comparing column names case-insensitively,

- they share at least :data:`MIN_SHARED` columns -- ids and audit columns
  alone are shared by unrelated tables -- and
- they share half of all their columns (Jaccard >= 0.5), or four fifths of
  the narrower table's columns while the wider has at most twice as many: a
  lookup table whose few columns were copied into a wide fact table is not a
  copy of it.

Names only, from discovery: no database access and no MCP here.

Comparing every pair is quadratic -- about a second at 2,000 tables, six at
5,000 -- so :class:`CopyIndex` uses a prefix filter, the standard exact
shortcut for set-similarity joins. Both rules above imply that a qualifying
pair shares at least ``max(3, ceil(0.4 * n))`` of the columns of each side,
*n* being that side's column count. With columns ordered rarest first, two
sets sharing at least *o* columns must share one among the first ``n - o + 1``
of each, so only tables meeting on one of their rarer columns are compared,
and none that qualify are missed.
"""

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

#: Shared columns below this never make two tables copies.
MIN_SHARED = 3

#: Near-copies named per table, closest first.
PER_TABLE = 3

#: Own columns listed per side; the rest are counted.
MAX_LISTED_COLUMNS = 8


@dataclass(frozen=True)
class NearCopy:
    """A table that shares most of its columns with another one."""

    #: The other table's identity (``schema.table``).
    table: str
    shared: int
    #: Columns only the first table has, in its column order.
    only_here: tuple[str, ...]
    #: Columns only :attr:`table` has, in its column order.
    only_there: tuple[str, ...]
    jaccard: float


def _min_shared(width: int) -> int:
    """The fewest columns a near-copy shares with a table this wide.

    ``max(3, ceil(0.4 * width))``, in integers. Jaccard >= 0.5 needs half of
    the union, so at least half of either side; containment needs four
    fifths of the narrower side, which is at least half the wider one, so at
    least two fifths of either.
    """
    return max(MIN_SHARED, (2 * width + 4) // 5)


def _similar(shared: int, width: int, other_width: int) -> bool:
    if shared < MIN_SHARED:
        return False
    union = width + other_width - shared
    narrow, wide = sorted((width, other_width))
    jaccard = 2 * shared >= union
    contained = 5 * shared >= 4 * narrow and wide <= 2 * narrow
    return jaccard or contained


class CopyIndex:
    """The tables of a catalog, indexed to find each one's near-copies.

    Built once per catalog; :meth:`of` then compares a table only with the
    tables that share one of its rarer columns.

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
        self._sets = {k: frozenset(cols) for k, cols in self._columns.items()}
        frequency = Counter(c for cols in self._columns.values() for c in cols)
        self._prefix: dict[str, list[str]] = {}
        self._postings: dict[str, list[str]] = defaultdict(list)
        for identity, columns in self._columns.items():
            width = len(columns)
            needed = _min_shared(width)
            if needed > width:
                continue  # too narrow to be anyone's near-copy
            rarest_first = sorted(columns, key=lambda c: (frequency[c], c))
            self._prefix[identity] = rarest_first[: width - needed + 1]
            for column in self._prefix[identity]:
                self._postings[column].append(identity)

    def __contains__(self, identity: object) -> bool:
        return identity in self._columns

    def of(self, identity: str, limit: int = PER_TABLE) -> list[NearCopy]:
        """The near-copies of *identity*, closest first.

        Args:
            identity: A table of this index.
            limit: How many to return at most.

        Returns:
            Up to *limit* near-copies; none for a table not in the index.
        """
        prefix = self._prefix.get(identity)
        if not prefix:
            return []
        mine = self._columns[identity]
        width = len(mine)
        sets = self._sets
        my_set = sets[identity]
        candidates = {other for column in prefix for other in self._postings[column]}
        candidates.discard(identity)
        found = []
        for other in candidates:
            their_set = sets[other]
            other_width = len(their_set)
            # Both rules need the wider at most twice the narrower.
            if width > 2 * other_width or other_width > 2 * width:
                continue
            shared = my_set & their_set
            if len(shared) < MIN_SHARED or not _similar(
                len(shared), width, other_width
            ):
                continue
            theirs = self._columns[other]
            found.append(
                NearCopy(
                    table=other,
                    shared=len(shared),
                    only_here=tuple(n for k, n in mine.items() if k not in shared),
                    only_there=tuple(n for k, n in theirs.items() if k not in shared),
                    jaccard=len(shared) / len(mine.keys() | theirs.keys()),
                )
            )
        found.sort(key=lambda c: (-c.jaccard, -c.shared, c.table))
        return found[:limit]

    def pairs(
        self, identities: Iterable[str], limit: int = PER_TABLE
    ) -> list[tuple[str, NearCopy]]:
        """Each near-copy pair with a table in *identities*, once, closest first.

        Args:
            identities: The tables whose near-copies are wanted.
            limit: Near-copies taken per table.

        Returns:
            ``(table, near_copy)`` pairs; a pair found from both sides once.
        """
        seen: set[frozenset[str]] = set()
        found: list[tuple[str, NearCopy]] = []
        for identity in identities:
            for copy in self.of(identity, limit):
                pair = frozenset((identity, copy.table))
                if pair not in seen:
                    seen.add(pair)
                    found.append((identity, copy))
        found.sort(key=lambda p: (-p[1].jaccard, -p[1].shared, p[0], p[1].table))
        return found


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
