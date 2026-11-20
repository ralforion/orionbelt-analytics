"""The name OrionBelt Analytics gives itself on the connections it opens.

Each database shows it next to the queries it runs: as the User-Agent on
BigQuery, ClickHouse over HTTP, Databricks and DuckDB (MotherDuck), as the
client name on ClickHouse's native protocol, as ``application_name`` on
PostgreSQL, and as the ``program_name`` connection attribute on MySQL.
Snowflake and Dremio get none: Snowflake's ``application`` is reserved for
its registered partners, and no Dremio setting has been checked to reach its
job history. OrionBelt Semantic Layer names itself the same way.
"""

from . import __version__

CLIENT_NAME = f"orionbelt-analytics/{__version__}"
