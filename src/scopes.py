"""OrionBelt Analytics' scopes, read from a verified token's claims.

Three scopes, each including the ones below it: ``oba:admin`` implies
``oba:write``, which implies ``oba:read``. Identity providers put scopes and
roles in different claims -- ``scope`` (a space-separated string), ``scp``,
``roles``, Keycloak's ``realm_access.roles``, Auth0's ``permissions`` -- and
name them in their own terms, so which claims to read and which of their
values grant what are settings (``MCP_AUTH_SCOPE_CLAIMS``,
``MCP_AUTH_SCOPE_MAP``), as in OrionBelt Semantic Layer.

Every tool has a tier (:data:`TOOL_SCOPES`); with ``MCP_AUTH_TOOL_SCOPES=true``
a call needs that tier's scope. Pure functions and data, no MCP or I/O.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

SCOPE_READ = "oba:read"
SCOPE_WRITE = "oba:write"
SCOPE_ADMIN = "oba:admin"

#: Each scope with the ones it implies.
IMPLIED: dict[str, frozenset[str]] = {
    SCOPE_READ: frozenset({SCOPE_READ}),
    SCOPE_WRITE: frozenset({SCOPE_WRITE, SCOPE_READ}),
    SCOPE_ADMIN: frozenset({SCOPE_ADMIN, SCOPE_WRITE, SCOPE_READ}),
}

#: Claims read for scopes and roles when ``MCP_AUTH_SCOPE_CLAIMS`` is not set.
DEFAULT_SCOPE_CLAIMS: tuple[str, ...] = ("scope", "scp", "roles")

#: The scope each tool needs: reading the database and the ontology, changing
#: what is stored about them, or clearing it.
TOOL_SCOPES: dict[str, str] = {
    # Reading. connect_database and discover_schema rewrite the connection's
    # cache, but a user who may query must be able to connect.
    "list_databases": SCOPE_READ,
    "list_schemas": SCOPE_READ,
    "connect_database": SCOPE_READ,
    "discover_schema": SCOPE_READ,
    "get_table_details": SCOPE_READ,
    "sample_table_data": SCOPE_READ,
    "execute_sql_query": SCOPE_READ,
    "generate_chart": SCOPE_READ,
    "download_artifact": SCOPE_READ,
    "suggest_semantic_names": SCOPE_READ,
    "get_semantic_model": SCOPE_READ,
    "list_semantic_models": SCOPE_READ,
    "graphrag_search": SCOPE_READ,
    "graphrag_query_context": SCOPE_READ,
    "graphrag_find_join_path": SCOPE_READ,
    "reachable_from": SCOPE_READ,
    "measurable_from": SCOPE_READ,
    "plan_composite_query": SCOPE_READ,
    "query_sparql": SCOPE_READ,
    # Writing what the server keeps: ontologies, names, reviews, models, RDF.
    "generate_ontology": SCOPE_WRITE,
    "apply_semantic_names": SCOPE_WRITE,
    "validate_relationship": SCOPE_WRITE,
    "review_ontology": SCOPE_WRITE,
    "confirm_join": SCOPE_WRITE,
    "load_my_ontology": SCOPE_WRITE,
    "save_semantic_model": SCOPE_WRITE,
    "add_semantic_context": SCOPE_WRITE,
    "store_ontology_in_rdf": SCOPE_WRITE,
    "add_rdf_knowledge": SCOPE_WRITE,
    # Clearing it.
    "reset_cache": SCOPE_ADMIN,
    "cleanup_workspace": SCOPE_ADMIN,
    "cleanup_old_versions": SCOPE_ADMIN,
}


def parse_scope_claims(raw: str) -> tuple[str, ...]:
    """``MCP_AUTH_SCOPE_CLAIMS``: comma-separated claim paths.

    Raises:
        ValueError: When it names no claim.
    """
    claims = tuple(part.strip() for part in raw.split(",") if part.strip())
    if not claims:
        raise ValueError("MCP_AUTH_SCOPE_CLAIMS names no claim.")
    return claims


def parse_scope_map(raw: str) -> dict[str, frozenset[str]]:
    """``MCP_AUTH_SCOPE_MAP``: comma-separated ``<IdP value>=<scope>`` pairs.

    A value may appear more than once to grant several scopes:
    ``analysts=oba:read,modelers=oba:write``.

    Raises:
        ValueError: On a malformed pair, or a scope that is not OrionBelt's.
    """
    result: dict[str, set[str]] = {}
    for pair in (p.strip() for p in raw.split(",")):
        if not pair:
            continue
        name, sep, scope = (part.strip() for part in pair.partition("="))
        if not sep or not name or not scope:
            raise ValueError(
                f"MCP_AUTH_SCOPE_MAP entry {pair!r} is not of the form "
                "<IdP value>=<scope>."
            )
        if scope not in IMPLIED:
            raise ValueError(
                f"MCP_AUTH_SCOPE_MAP maps {name!r} to {scope!r}, which is not an "
                f"OrionBelt scope (expected one of: {', '.join(sorted(IMPLIED))})."
            )
        result.setdefault(name, set()).add(scope)
    return {name: frozenset(scopes) for name, scopes in result.items()}


def claim_at(claims: Mapping[str, Any], path: str) -> Any:
    """The claim *path* names, or None.

    A claim of exactly that name first: Auth0 and others namespace custom
    claims with a URL (``https://example.com/roles``), dots and all. Else a dot
    path into nested objects (``realm_access.roles``, Keycloak).
    """
    if path in claims:
        return claims[path]
    value: Any = claims
    for part in path.split("."):
        if not isinstance(value, Mapping):
            return None
        value = value.get(part)
    return value


def _values(raw: Any) -> Iterable[str]:
    """A claim's values: a space-separated string (``scope``) or a list."""
    if isinstance(raw, str):
        return raw.split()
    if isinstance(raw, list):
        return (item for item in raw if isinstance(item, str))
    return ()


def scopes_from_claims(
    claims: Mapping[str, Any],
    scope_claims: Iterable[str] = DEFAULT_SCOPE_CLAIMS,
    scope_map: Mapping[str, frozenset[str]] | None = None,
) -> list[str]:
    """The scopes a verified token grants.

    Every value of every claim in *scope_claims*, as it is -- so a setting
    naming an identity provider's own scope (``MCP_AUTH_REQUIRED_SCOPES``)
    still matches -- plus the OrionBelt scopes: a value that is one counts as
    itself, a value in *scope_map* grants what it maps to, and each brings
    the scopes it implies.

    Returns:
        The scopes, sorted, each once.
    """
    granted: set[str] = set()
    for path in scope_claims:
        for value in _values(claim_at(claims, path)):
            granted.add(value)
            granted |= IMPLIED.get(value, frozenset())
            for scope in (scope_map or {}).get(value, ()):
                granted |= IMPLIED[scope]
    return sorted(granted)
