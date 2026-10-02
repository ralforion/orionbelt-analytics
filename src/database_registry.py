"""Databases this server can connect to, by name.

A server used to hold one connection per database type, read from that type's
variables (``DATABRICKS_SERVER_HOSTNAME``, ``POSTGRES_HOST``, ...). A person
asking about "the finance-2025 database" had no way to say which one, and the
model none to find out what was there.

Named connections are declared in the environment::

    OBA_DATABASES=finance-2025,sales
    DB_FINANCE_2025_TYPE=databricks
    DB_FINANCE_2025_DESCRIPTION=Finance actuals 2025, Unity Catalog finance.gold
    DB_FINANCE_2025_DATABRICKS_CATALOG=finance
    DB_FINANCE_2025_DATABRICKS_SCHEMA=gold

Each one is read with the type's usual variable names behind a ``DB_<NAME>_``
prefix. A variable it does not set falls back to the unprefixed one, so two
connections can share a workspace and its token and differ only in catalog or
schema. A type configured the old way, without a name, is still listed, under
the type's name.

No MCP dependencies: importable and testable on its own.
"""

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import overload

from .constants import SUPPORTED_DB_TYPES

DATABASES_VARIABLE = "OBA_DATABASES"

# The variable whose presence means a type is configured without a name.
_PRESENCE_VARIABLES: dict[str, tuple[str, ...]] = {
    "postgresql": ("POSTGRES_HOST",),
    "mysql": ("MYSQL_HOST",),
    "snowflake": ("SNOWFLAKE_ACCOUNT",),
    "dremio": ("DREMIO_URI", "DREMIO_HOST"),
    "clickhouse": ("CLICKHOUSE_HOST",),
    "bigquery": ("BIGQUERY_PROJECT_ID",),
    "duckdb": ("DUCKDB_DATABASE_PATH", "MOTHERDUCK_TOKEN"),
    "databricks": ("DATABRICKS_SERVER_HOSTNAME",),
}

# What may be shown about a connection: where it points, never how it signs
# in. A whitelist, so a variable added to a driver later stays hidden until
# someone decides it is safe to show.
_DISPLAY_VARIABLES: dict[str, tuple[str, ...]] = {
    "postgresql": ("POSTGRES_DATABASE",),
    "mysql": ("MYSQL_DATABASE",),
    "snowflake": ("SNOWFLAKE_DATABASE", "SNOWFLAKE_SCHEMA"),
    "dremio": (),
    "clickhouse": ("CLICKHOUSE_DATABASE",),
    "bigquery": ("BIGQUERY_PROJECT_ID", "BIGQUERY_DATASET"),
    "duckdb": (),
    "databricks": ("DATABRICKS_CATALOG", "DATABRICKS_SCHEMA"),
}


def env_key(name: str) -> str:
    """The variable prefix part for a connection name: ``finance-2025`` -> ``FINANCE_2025``.

    Args:
        name: The connection's name.

    Returns:
        The name in upper case, every run of other characters one underscore.
    """
    return re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")


def _normalized(name: str) -> str:
    """A name as compared: case, spaces, dashes and underscores do not matter."""
    return re.sub(r"[\s_\-]+", "", name.casefold())


@dataclass(frozen=True)
class DatabaseEntry:
    """One database the server can connect to.

    Attributes:
        name: What a person calls it; the type's name for an unnamed one.
        db_type: One of the supported database types.
        description: What it holds, for a model choosing between several.
        prefix: The variable prefix for a named connection; empty if unnamed.
    """

    name: str
    db_type: str
    description: str = ""
    prefix: str = ""
    environ: Mapping[str, str] = field(
        default_factory=lambda: os.environ, repr=False, compare=False
    )

    @property
    def named(self) -> bool:
        """Whether it was declared by name rather than configured by type."""
        return bool(self.prefix)

    @overload
    def getenv(self, variable: str) -> str | None: ...

    @overload
    def getenv(self, variable: str, default: str) -> str: ...

    def getenv(self, variable: str, default: str | None = None) -> str | None:
        """A setting for this connection, with the unprefixed one as fallback.

        Args:
            variable: The type's usual variable name, e.g. ``DATABRICKS_SCHEMA``.
            default: What to return if neither is set.

        Returns:
            The value.
        """
        if self.prefix:
            value = self.environ.get(f"{self.prefix}{variable}")
            if value is not None:
                return value
        return self.environ.get(variable, default)

    def describe(self) -> dict[str, str]:
        """What may be shown about it: name, type, description and target.

        Returns:
            A dictionary with no credentials in it.
        """
        shown = {"name": self.name, "type": self.db_type}
        if self.description:
            shown["description"] = self.description
        for variable in _DISPLAY_VARIABLES.get(self.db_type, ()):
            value = self.getenv(variable)
            if value:
                # DATABRICKS_CATALOG -> catalog
                shown[variable.split("_", 1)[1].lower()] = value
        return shown


class DatabaseConfigError(ValueError):
    """The named connections in the environment are not usable as written."""


def configured_databases(
    environ: Mapping[str, str] | None = None,
) -> list[DatabaseEntry]:
    """Every database this server is configured for.

    Named connections first, in the order ``OBA_DATABASES`` lists them, then
    each type configured without a name -- unless that type also has named
    connections, whose shared defaults its variables then are.

    Args:
        environ: Where to read the settings; the process environment by default.

    Returns:
        The configured databases.

    Raises:
        DatabaseConfigError: If a named connection has no type, an unsupported
            one, or shares its name with another.
    """
    env = os.environ if environ is None else environ
    entries: list[DatabaseEntry] = []
    seen: set[str] = set()

    for raw in env.get(DATABASES_VARIABLE, "").split(","):
        name = raw.strip()
        if not name:
            continue
        key = _normalized(name)
        if key in seen:
            raise DatabaseConfigError(
                f"Database name '{name}' appears twice in {DATABASES_VARIABLE}"
            )
        seen.add(key)
        prefix = f"DB_{env_key(name)}_"
        db_type = env.get(f"{prefix}TYPE", "").strip().lower()
        if not db_type:
            raise DatabaseConfigError(
                f"Database '{name}' has no type: set {prefix}TYPE to one of "
                f"{', '.join(SUPPORTED_DB_TYPES)}"
            )
        if db_type not in SUPPORTED_DB_TYPES:
            raise DatabaseConfigError(
                f"Database '{name}' has unsupported type '{db_type}' "
                f"({prefix}TYPE); use one of {', '.join(SUPPORTED_DB_TYPES)}"
            )
        entries.append(
            DatabaseEntry(
                name=name,
                db_type=db_type,
                description=env.get(f"{prefix}DESCRIPTION", "").strip(),
                prefix=prefix,
                environ=env,
            )
        )

    # A type's unprefixed variables are the defaults its named connections
    # share. Once a type has a named connection they are not a database of
    # their own: listing one would offer the model a target nobody named.
    named_types = {entry.db_type for entry in entries}
    for db_type in SUPPORTED_DB_TYPES:
        if _normalized(db_type) in seen or db_type in named_types:
            continue
        if any(env.get(v) for v in _PRESENCE_VARIABLES.get(db_type, ())):
            entries.append(DatabaseEntry(name=db_type, db_type=db_type, environ=env))

    return entries


def unnamed_entry(
    db_type: str, environ: Mapping[str, str] | None = None
) -> DatabaseEntry:
    """The connection for a type configured without a name.

    Args:
        db_type: The database type.
        environ: Where to read the settings; the process environment by default.

    Returns:
        An entry reading the type's unprefixed variables.
    """
    return DatabaseEntry(
        name=db_type,
        db_type=db_type,
        environ=os.environ if environ is None else environ,
    )


def find_database(name: str, entries: list[DatabaseEntry]) -> DatabaseEntry | None:
    """The configured database a person means by *name*.

    Case, spaces, dashes and underscores are ignored, so ``Finance 2025`` finds
    ``finance-2025``.

    Args:
        name: The name as given.
        entries: The configured databases.

    Returns:
        The match, or None.
    """
    wanted = _normalized(name)
    for entry in entries:
        if _normalized(entry.name) == wanted:
            return entry
    return None
