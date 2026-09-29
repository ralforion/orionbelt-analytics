"""How GraphRAG names a table, so two schemas can hold the same one.

The relationship graph keyed nodes by bare table name, and vector ids were
built from the same names, so `sales.orders` and `archive.orders` were one
node with one set of vectors: whichever schema was discovered last described
both. This module holds the identity that fixes it, and the resolution that
keeps bare names working for everyone who sends them.

A table with no schema keeps its bare name. Tests, batch use and an
unqualified DuckDB connection all work without schemas, and there is nothing
ambiguous about them, so the change is confined to the case that is actually
ambiguous.
"""

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


def quote_part(part: str) -> str:
    """One component of an identity, quoted if it could be misread.

    A dot inside a name is legal SQL -- ``"sales.eu".orders`` and
    ``sales."eu.orders"`` are two different tables -- and joined with a bare
    dot both became ``sales.eu.orders``, one silently overwriting the other.
    Such a component is written the way SQL writes it, in double quotes with
    inner quotes doubled. Every other name is left exactly as it was, so
    ordinary identities do not change.

    Args:
        part: A schema, table or column name.

    Returns:
        The name, quoted only when it contains a dot or a double quote.
    """
    if "." in part or '"' in part:
        return '"' + part.replace('"', '""') + '"'
    return part


def qualified(schema: str | None, table: str) -> str:
    """The identity of a table within this index.

    Args:
        schema: Schema the table was discovered under, or None.
        table: The table's own name.

    Returns:
        ``schema.table``, or just the table when there is no schema, each
        component quoted when it would otherwise be ambiguous.
    """
    if not schema:
        return quote_part(table)
    return f"{quote_part(schema)}.{quote_part(table)}"


def _components(identity: str) -> list[str]:
    """Split an identity on the dots that are not inside quotes, unquoting.

    Args:
        identity: What :func:`qualified` produced.

    Returns:
        Its components, with quoting removed.
    """
    components: list[str] = []
    current: list[str] = []
    quoted = False
    position = 0
    while position < len(identity):
        char = identity[position]
        if quoted:
            if char == '"':
                if identity[position + 1 : position + 2] == '"':
                    current.append('"')
                    position += 2
                    continue
                quoted = False
            else:
                current.append(char)
        elif char == '"':
            quoted = True
        elif char == ".":
            components.append("".join(current))
            current = []
        else:
            current.append(char)
        position += 1
    components.append("".join(current))
    return components


def split(identity: str) -> tuple[str | None, str]:
    """Take an identity apart again.

    Splits on the first dot outside quotes. More than two unquoted components
    can only come from a name written before quoting existed; the first is the
    schema and the rest the table, as before.

    Args:
        identity: What :func:`qualified` produced.

    Returns:
        The schema (or None) and the table name, both unquoted.
    """
    components = _components(identity)
    if len(components) == 1:
        return None, components[0]
    return components[0], ".".join(components[1:])


def display_name(identity: str) -> str:
    """The table name a person asked about, without its schema.

    Args:
        identity: What :func:`qualified` produced.

    Returns:
        The bare table name.
    """
    return split(identity)[1]


@dataclass
class Resolution:
    """What a name given to a tool turned out to mean."""

    identity: str | None
    candidates: list[str]

    @property
    def found(self) -> bool:
        """Whether the name named exactly one table."""
        return self.identity is not None

    @property
    def ambiguous(self) -> bool:
        """Whether the name named more than one table."""
        return self.identity is None and len(self.candidates) > 1


def choose(
    name: str,
    is_identity: bool,
    candidates: list[str],
    current_schema: str | None = None,
) -> Resolution:
    """Decide which table a name refers to, given what shares that name.

    Models send bare names, and a bare name is usually unambiguous. When it is
    not, that is worth saying rather than deciding silently in favour of
    whichever schema was discovered last, which is what used to happen.

    Order: an exact identity wins; then a unique bare match; then a match in
    the schema the session is working in. Anything else is ambiguous and
    reported with its candidates.

    Takes the answers rather than the index, so a caller that keeps a name
    index does not have to walk every table to use the same rules.

    Args:
        name: What the caller wrote, bare or qualified.
        is_identity: Whether *name* is itself a table in the index.
        candidates: The identities whose table name is *name*.
        current_schema: The session's schema, used to break a tie.

    Returns:
        The resolution, which may hold no identity and several candidates.
    """
    if is_identity:
        return Resolution(name, [name])

    ordered = sorted(candidates)
    if len(ordered) == 1:
        return Resolution(ordered[0], ordered)
    if not ordered:
        return Resolution(None, [])

    if current_schema is not None:
        preferred = qualified(current_schema, name)
        if preferred in ordered:
            return Resolution(preferred, ordered)

    logger.info(f"'{name}' names more than one table: {', '.join(ordered)}")
    return Resolution(None, ordered)


def resolve(
    name: str,
    known: set[str] | dict[str, object],
    current_schema: str | None = None,
) -> Resolution:
    """Work out which table a name refers to, by walking what is known.

    A convenience over :func:`choose` for callers with nothing but the set of
    identities. A caller in a hot path keeps a name index and calls ``choose``.

    Args:
        name: What the caller wrote, bare or qualified.
        known: The identities in the index.
        current_schema: The session's schema, used to break a tie.

    Returns:
        The resolution.
    """
    identities = set(known)
    return choose(
        name,
        name in identities,
        [identity for identity in identities if display_name(identity) == name],
        current_schema,
    )
