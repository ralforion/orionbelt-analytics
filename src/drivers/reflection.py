"""Reflecting a whole schema in one pass, for the plain SQLAlchemy drivers.

Discovery asked the database about one table at a time: a connection, a fresh
``Inspector`` and four reflection calls each (``has_table``, ``get_columns``,
``get_pk_constraint``, ``get_foreign_keys``). A hundred tables meant a hundred
connections, a hundred inspectors -- each discarding the reflection cache the
last one had filled -- and some four hundred round trips.

SQLAlchemy 2.0's ``get_multi_*`` answers for every table in a schema at once,
and a dialect that cannot do so falls back internally to the same per-table
calls, so this is never worse. One connection and one Inspector serve the whole
batch either way.

The metadata is assembled exactly as each driver's own ``analyze_table``
assembles it -- the same case-insensitive primary-key and foreign-key matching,
the same ``str(type)`` -- and a test asserts the two agree table by table.
"""

import logging
from typing import Any, cast

from sqlalchemy import inspect
from sqlalchemy.engine import Engine

from ..database_manager import ColumnInfo, TableInfo

logger = logging.getLogger(__name__)


def build_table_info(
    table_name: str,
    schema_label: str,
    table_columns: list[dict[str, Any]],
    table_pk: dict[str, Any] | None,
    table_fks: list[dict[str, Any]],
) -> TableInfo:
    """Assemble one table's metadata from reflected parts.

    Args:
        table_name: The table's name.
        schema_label: What to record as the table's schema, which each driver
            spells its own way ("public", "main", or the empty string).
        table_columns: Reflected columns.
        table_pk: Reflected primary-key constraint, if any.
        table_fks: Reflected foreign-key constraints.

    Returns:
        The table's metadata.
    """
    primary_keys = table_pk.get("constrained_columns", []) if table_pk else []
    primary_keys_upper = [pk.upper() for pk in primary_keys]

    columns = []
    foreign_keys: list[dict[str, Any]] = []
    for col_info in table_columns:
        column_name = col_info["name"]
        is_pk = column_name.upper() in primary_keys_upper

        fk_table = None
        fk_column = None
        is_fk = False
        for fk in table_fks:
            constrained_cols_upper = [
                c.upper() for c in fk.get("constrained_columns", [])
            ]
            if column_name.upper() in constrained_cols_upper:
                is_fk = True
                fk_idx = constrained_cols_upper.index(column_name.upper())
                fk_table = fk.get("referred_table")
                referred_cols = fk.get("referred_columns", [])
                fk_column = (
                    referred_cols[fk_idx] if fk_idx < len(referred_cols) else None
                )
                fk_schema = fk.get("referred_schema")
                if fk_table:
                    foreign_keys.append(
                        {
                            "column": column_name,
                            "referenced_table": fk_table,
                            "referenced_column": fk_column,
                            "referenced_schema": fk_schema,
                        }
                    )
                break

        columns.append(
            ColumnInfo(
                name=column_name,
                data_type=str(col_info["type"]),
                is_nullable=col_info["nullable"],
                is_primary_key=is_pk,
                is_foreign_key=is_fk,
                foreign_key_table=fk_table,
                foreign_key_column=fk_column,
                comment=col_info.get("comment"),
            )
        )

    return TableInfo(
        name=table_name,
        schema=schema_label,
        columns=columns,
        primary_keys=primary_keys,
        foreign_keys=foreign_keys,
        comment=None,
        row_count=None,
        sample_data=None,
    )


def reflect_tables(
    engine: Engine,
    table_names: list[str],
    schema_name: str | None,
    schema_label: str,
) -> dict[str, TableInfo]:
    """Reflect several tables through one connection and one Inspector.

    Args:
        engine: The connected engine.
        table_names: Tables to reflect.
        schema_name: Schema to ask about, or None for the connection's default.
        schema_label: What to record as each table's schema.

    Returns:
        Metadata by table name, without tables the database did not report.

    Raises:
        Exception: Whatever the reflection raised. The caller falls back to
            reflecting one table at a time, which is the older path and copes
            with dialects this one does not suit.
    """
    wanted = set(table_names)
    analyzed: dict[str, TableInfo] = {}

    with engine.connect() as conn:
        inspector = inspect(conn)
        columns_by_table = inspector.get_multi_columns(schema=schema_name)
        pks_by_table = inspector.get_multi_pk_constraint(schema=schema_name)
        fks_by_table = inspector.get_multi_foreign_keys(schema=schema_name)

        for key, table_columns in columns_by_table.items():
            # Keys are (schema, table); the schema part is the dialect's own
            # spelling and may differ from what was asked for.
            name = key[1] if isinstance(key, tuple) else key
            if name not in wanted:
                continue
            analyzed[name] = build_table_info(
                name,
                schema_label,
                cast(list[dict[str, Any]], list(table_columns)),
                cast("dict[str, Any] | None", pks_by_table.get(key)),
                cast(list[dict[str, Any]], list(fks_by_table.get(key) or [])),
            )

    missing = wanted - analyzed.keys()
    if missing:
        logger.debug(
            f"Schema reflection did not report {len(missing)} requested "
            f"table(s): {sorted(missing)}"
        )
    return analyzed
