"""Columns that say how a row is kept rather than what it holds.

Shared by near-copy detection, where they are no evidence that two tables are
copies, and bridge detection, where they are nothing a link table holds of its
own. Kept apart from both so that neither pulls in the other's package.
"""

import re

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
