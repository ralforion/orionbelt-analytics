"""Names a query may have meant, for an OBQC error about one it got wrong.

``SELECT * FROM customer`` against a table ``customers`` was answered with
the first ten tables of the ontology, in whatever order it stored them --
often not including ``customers``. Here the wrong name is compared with the
names it could have meant, so the error can say which.

Deterministic and local: only the ontology's own names and the business
names it gives them (``rdfs:label``, as ``apply_semantic_names`` sets them)
are compared, never anything learned elsewhere. A name is offered when it is
the same name written differently (``OrderItems`` for ``order_items``), the
same words in another number or order (``customer`` for ``customers``),
abbreviated word by word (``cust_id`` for ``customer_id``, ``qty`` for
``quantity``), with words left out or added (``price`` for ``unit_price``,
``customer_name`` for ``name``), or close in spelling (``custmer``).
Synonyms (``client`` for ``customers``) are not found.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from difflib import SequenceMatcher

from .join_discovery import abbreviates, name_words, singular

#: How close a name must be to be offered at all. ``price`` for
#: ``unit_price`` is 0.8; ``id`` for ``ip`` (0.5) is not offered.
CUTOFF = 0.75
#: Names offered at most, best first.
LIMIT = 3
#: How far behind the best a name may be and still be offered, so a name
#: written differently is not followed by names merely spelled alike.
WINDOW = 0.1

_NOT_ALPHANUMERIC = re.compile(r"[^0-9a-z]+")


@dataclass(frozen=True)
class Match:
    """A name the query may have meant."""

    #: As the query should write it: ``customers`` or ``customers.name``.
    name: str
    #: The business name it was found by, when that, not the name, matched.
    label: str | None = None
    score: float = 0.0

    def __str__(self) -> str:
        return f'{self.name} ("{self.label}")' if self.label else self.name


def _flat(name: str) -> str:
    return _NOT_ALPHANUMERIC.sub("", name.lower())


def _abbreviated(a: str, b: str) -> bool:
    # Three letters are enough among a table's own names, as they are against
    # the dictionary in join discovery: the candidates are few.
    return abbreviates(a, b, in_order_from=3) or abbreviates(b, a, in_order_from=3)


def similarity(wrong: str, name: str) -> float:
    """How likely *wrong* was meant to be *name*, from 0 to 1."""
    flat_wrong, flat_name = _flat(wrong), _flat(name)
    if not flat_wrong or not flat_name:
        return 0.0
    if flat_wrong == flat_name:
        return 1.0
    a = [singular(w) for w in name_words(wrong)]
    b = [singular(w) for w in name_words(name)]
    if a == b:
        return 0.97
    if sorted(a) == sorted(b):
        return 0.93
    if len(a) == len(b) and all(x == y or _abbreviated(x, y) for x, y in zip(a, b)):
        return 0.9
    spelling = max(
        SequenceMatcher(None, flat_wrong, flat_name).ratio(),
        SequenceMatcher(None, "".join(a), "".join(b)).ratio(),
    )
    # Every word of the shorter in the longer: a word left out or added. The
    # shared words must say something -- "id" alone is in every key.
    shorter, longer = sorted((set(a), set(b)), key=len)
    if shorter <= longer and sum(len(w) for w in shorter) >= 3:
        return max(spelling, 0.8)
    return spelling


def closest(
    wrong: str,
    candidates: Iterable[tuple[str, str, str | None]],
    limit: int = LIMIT,
) -> list[Match]:
    """The candidates *wrong* most likely meant, best first.

    Args:
        wrong: The name as the query wrote it, without its qualifier.
        candidates: ``(compared, written, label)`` triples: the name to
            compare *wrong* with, how the query should write it, and its
            business name or None. Order breaks no ties; names do.
        limit: Names returned at most.

    Returns:
        Up to *limit* matches at CUTOFF or above and within WINDOW of the
        best one; none when nothing is close.
    """
    found: dict[str, Match] = {}
    for compared, written, label in candidates:
        by_name = similarity(wrong, compared)
        by_label = similarity(wrong, label) if label else 0.0
        match = (
            Match(written, label, by_label)
            if by_label > by_name
            else Match(written, None, by_name)
        )
        if match.score >= CUTOFF and (
            written not in found or match.score > found[written].score
        ):
            found[written] = match
    ranked = sorted(found.values(), key=lambda m: (-m.score, m.name))
    if not ranked:
        return []
    best = ranked[0].score
    return [m for m in ranked if m.score >= best - WINDOW][:limit]


def did_you_mean(matches: list[Match]) -> str | None:
    """``Did you mean orders or order_items?``, or None without matches."""
    if not matches:
        return None
    names = [str(m) for m in matches]
    listed = names[0] if len(names) == 1 else f"{', '.join(names[:-1])} or {names[-1]}"
    return f"Did you mean {listed}?"


def listing(kind: str, names: Iterable[str], shown: int = 10) -> str:
    """``Available tables: a, b, c (40 in all)``, sorted, the first *shown*."""
    every = sorted(set(names), key=str.lower)
    head = ", ".join(every[:shown])
    more = f" ({len(every)} in all)" if len(every) > shown else ""
    return f"Available {kind}: {head}{more}"
