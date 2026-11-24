"""Constrained parameter types for MCP tool signatures.

These bound input size and shape at the MCP boundary so invalid or oversized
arguments are rejected before reaching a handler. They are defense-in-depth and
a published-schema hint for hosts; handlers keep their own runtime validation.
(Surfaced by an mcp-security-audit run.)
"""

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from .constants import SUPPORTED_DB_TYPES

# Database type literal, derived from the canonical supported list so it can
# never drift from SUPPORTED_DB_TYPES / the driver registry.
_DbType = Literal[tuple(SUPPORTED_DB_TYPES)]  # type: ignore[valid-type]
# Database/schema/table/column identifiers.
_Identifier = Annotated[str, Field(max_length=255)]
# Short natural-language text (titles, query intents).
_ShortText = Annotated[str, Field(max_length=1000)]
# Natural-language search queries.
_QueryText = Annotated[str, Field(max_length=4000)]
# Filenames / model names — no path separators (prevents traversal).
_SafeName = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[^/\\]+$")]
# A filesystem folder path.
_FolderPath = Annotated[str, Field(max_length=1024)]
# Raw SQL / SPARQL query bodies.
_QueryBody = Annotated[str, Field(max_length=100_000)]
# Large document payloads (YAML / TTL / JSON).
_DocBody = Annotated[str, Field(max_length=5_000_000)]
# URIs.
_Uri = Annotated[str, Field(max_length=2048)]


class ReviewItem(BaseModel):
    """One table, column or relationship a person reviewed (review_ontology)."""

    table: _Identifier = Field(description="The table, e.g. 'orders'")
    column: _Identifier | None = Field(
        default=None,
        description="A column of it; with to_table, the key of a relationship",
    )
    to_table: _Identifier | None = Field(
        default=None,
        description="For a relationship: the table the column references",
    )
    to_column: _Identifier | None = Field(
        default=None,
        description=(
            "For a relationship: the referenced column, when the column "
            "references the table by more than one key"
        ),
    )
    status: Literal["reviewed", "rejected", "draft"] = Field(
        default="reviewed",
        description=(
            "reviewed: checked and kept; rejected: wrong (a rejected "
            "relationship is no join); draft: remove an earlier review"
        ),
    )
    note: _ShortText | None = Field(default=None, description="Why, optionally")
