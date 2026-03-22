# Changelog

All notable changes to OrionBelt Analytics will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.0] - 2026-03-22

### Added
- **MySQL Support** - Full support for MySQL 5.7+, 8.0+, and MariaDB 10.3+
- New MySQL database driver: `mysql.py` with PyMySQL connector
- Connection method: `connect_mysql()` with charset configuration (default: utf8mb4)
- MySQL configuration section in `.env.template` with troubleshooting guide
- MySQL system schema exclusions: `information_schema`, `mysql`, `performance_schema`, `sys`
- MySQL badge and documentation in README
- Connection pooling with automatic reconnection for MySQL (pool_pre_ping=True)

### Changed
- Supported databases expanded from 7 to 8
- README updated with MySQL in all database lists
- Version bumped to 1.1.0
- Project keywords expanded to include `mysql`

### Dependencies
- Added `pymysql>=1.1.0` for MySQL connectivity (pure Python, cross-platform)

### Database Support Summary
OrionBelt Analytics v1.1.0 now supports:
1. PostgreSQL
2. **MySQL** (NEW)
3. Snowflake
4. ClickHouse
5. Dremio
6. BigQuery
7. DuckDB/MotherDuck
8. Databricks SQL

## [1.0.0] - 2026-03-16

### Added
- **BigQuery Support** - Full support for Google BigQuery with service account authentication
- **DuckDB/MotherDuck Support** - Local DuckDB files and MotherDuck cloud database support
- **Databricks SQL Support** - Databricks SQL Warehouse and Unity Catalog integration
- New database drivers: `bigquery.py`, `duckdb.py`, `databricks.py`
- Connection methods: `connect_bigquery()`, `connect_duckdb()`, `connect_databricks()`
- Configuration templates for all new databases in `.env.template`
- Comprehensive troubleshooting guides for BigQuery, DuckDB/MotherDuck, and Databricks
- Database-specific system schema exclusions for all vendors
- Updated README with badges, examples, and documentation for all 7 databases
- Connection test examples for new databases

### Changed
- **FastMCP upgraded to 3.1+** - Updated from 3.0.2 to >=3.1.0 for latest features
- Development Status upgraded to "Production/Stable" (was "Beta")
- README badge updated to reflect FastMCP 3.1+
- Copyright year updated to 2025-2026
- "Better Together" section now lists all 7 supported databases
- Key dependencies documentation updated to include all database connectors
- Project keywords expanded to include bigquery, duckdb, databricks

### Dependencies
- Added `sqlalchemy-bigquery>=1.11.0` for BigQuery support
- Added `duckdb>=1.1.0` and `duckdb-engine>=0.13.0` for DuckDB support
- Added `databricks-sql-connector>=3.5.0` for Databricks support
- Updated `fastmcp>=3.1.0` (was >=3.0.2)

### Compatibility
- Fully compatible with OrionBelt Semantic Layer v1.0.0
- Fully compatible with OrionBelt Semantic Layer MCP v1.0.0
- All three platform components now support the same 7 database vendors

### Database Support Summary
OrionBelt Analytics v1.0.0 now supports:
1. PostgreSQL
2. Snowflake
3. ClickHouse
4. Dremio
5. **BigQuery** (NEW)
6. **DuckDB/MotherDuck** (NEW)
7. **Databricks SQL** (NEW)

## [0.7.0] - 2024

### Added
- SPARQL query support with 7 SPARQL tools
- GraphRAG integration for schema discovery
- Comprehensive ontology generation
- RDF/OWL support with Oxigraph storage

### Changed
- Enhanced documentation
- Improved repository structure

## [0.6.0] - 2024

### Added
- Initial release with PostgreSQL, Snowflake, ClickHouse, and Dremio support
- FastMCP 3.0 integration
- Ontology generation
- Schema analysis tools

[1.0.0]: https://github.com/ralfbecher/orionbelt-analytics/releases/tag/v1.0.0
[0.7.0]: https://github.com/ralfbecher/orionbelt-analytics/releases/tag/v0.7.0
[0.6.0]: https://github.com/ralfbecher/orionbelt-analytics/releases/tag/v0.6.0
