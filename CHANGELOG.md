# Changelog

All notable changes to OrionBelt Analytics will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.5.3] - 2026-06-10

### Changed
- **Constrained MCP tool string parameters at the input boundary.** Enumerated parameters now use `Literal` types (`db_type`, `cache_type`, `artifact_type`, `source`, `chart_type`, `chart_style`, `sort_order`, `output_format`, `element_type`), free-text and identifier parameters carry `max_length` bounds, and filename/model-name parameters reject path separators — so invalid, oversized, or path-traversing arguments are rejected before reaching a handler, and the constraints are published in each tool's JSON schema. Handlers keep their existing runtime validation as defense-in-depth. `save_semantic_model` additionally reduces `model_name` to a bare filename component so it can never escape the models directory.

### Fixed
- **Aligned all documentation with the registered tool set.** Removed references to four tools that were advertised but never registered (`validate_sql_syntax`, `download_ontology`, `list_tables_sparql`, `diagnose_connection_issue`) across the README, `docs/`, Claude skills, and all 8 integration guides, replacing them with registered equivalents (`execute_sql_query`'s built-in validation, `download_artifact`, `query_sparql`). Completed `docs/tools-reference.md` to cover all 23 tools (was 16). Corrected stale tool counts (README 32, startup banner 22 → 23).
- **Resolved all pre-existing lint findings** (ruff: unused imports, f-strings without placeholders, unused locals, redundant imports; intentional late imports annotated with `# noqa: E402`). `ruff check .` is now clean.

### Added
- `scripts/bump-version.sh` — bumps the version across all files, inserts a CHANGELOG stub, and runs `uv lock` in one step.
- `scripts/publish-docker.sh` accepts a generic `DOCKERHUB_PAT` (legacy `DOCKERHUB_RALFORION_PAT` still works).

## [1.5.2] - 2026-06-08

### Removed
- **`get_server_info` MCP tool** -- removed as redundant. Server metadata (name, version, supported databases, capabilities) is already provided via the MCP `initialize` handshake and the server `instructions`, and the live tool list via `tools/list`. The genuinely-unique capability descriptions were folded into the server instructions.

### Changed
- **Condensed server `instructions`** (~58 -> ~30 lines) -- dropped the duplicated database list and `Version:` footer (already supplied via `FastMCP(version=...)`), merged overlapping capability sections.
- **Slimmed `generate_chart`** from 13 to 11 parameters -- removed `width`/`height`. Interactive charts are responsive and size to their container; static PNG export now uses fixed 800x600 constants. Docstring condensed.

## [1.5.1] - 2026-06-06

### Fixed
- **MCP handshake advertised the wrong version** -- `FastMCP()` was constructed without an explicit `version`, so the `initialize` response's `serverInfo.version` fell back to the FastMCP package version (e.g. `3.2.4`) instead of the application version. The constructor now receives `version=__version__`.

### Changed
- Upgraded `fastmcp[apps]` from `>=3.2.4` to `>=3.3.1,<3.4`.

## [1.5.0] - 2026-05-03

### Added
- **MCP sampling for `suggest_semantic_names`** -- when the connected client advertises the sampling capability, the server now calls back through the host LLM via `ctx.sample()` to pre-fill rename suggestions for cryptic identifiers, returning a `suggestions` dict alongside the cryptic-name lists in a single tool call.
  - Gated on a new `ENABLE_SAMPLING` env flag (default `true`). Set to `false` to force the legacy manual-review path everywhere.
  - Clients without sampling support (e.g. Claude Desktop) silently fall back to the legacy response shape — no breaking change.
  - Sampling requests, results, and failures are logged with elapsed time and item counts for observability.

### Fixed
- **MCP session crash on client disconnect during `suggest_semantic_names`** -- a notification (`ctx.info`) write that hit `anyio.ClosedResourceError` because the client had already closed the streamable-HTTP session was caught by the handler's outer `except` and triggered a second doomed write, bringing down the entire FastMCP TaskGroup. Notifications are now sent through `safe_ctx_info` (failures swallowed at debug level), and `ClosedResourceError`-class disconnects re-raise cleanly so the framework tears the session down instead of writing into a dead transport.
- **Sampling response parsing** -- replaced pydantic-ai's `result_type=Dict[str, str]` (which forces the model to call an injected `final_response` tool, fragile on large responses) with explicit JSON parsing that accepts bare JSON, ```json fences, and prose-embedded JSON.

## [1.2.0] - 2026-04-05

### Changed
- **Migrated to official FastMCP Apps standard** - Replaced `mcp-ui-server` community library with native FastMCP Apps support
  - Charts now use official `ui://` resource URI pattern with `AppConfig`
  - Chart viewer configured with `ResourceCSP` for CDN security (Plotly, unpkg)
  - Full compatibility with Claude Desktop, Claude.ai, ChatGPT, VS Code, and Goose
  - Cleaner implementation using standard MCP Apps protocol
- Upgraded `fastmcp` dependency from `>=3.1.0` to `fastmcp[apps]>=3.1.0`

### Removed
- Dependency on `mcp-ui-server>=1.0.0` (replaced by official FastMCP Apps)

## [1.1.3] - 2026-03-28

### Fixed
- Clear OBQC validator on connection change to prevent cross-database validation
- Clear Oxigraph store on connection change to prevent cross-connection RDF contamination
- Extend `_reconnect()` for BigQuery, DuckDB, Databricks, and MySQL backends
- Add DREMIO_URI + DREMIO_PAT auth support to main connect_database handler

### Added
- 11 regression tests for code review findings

## [1.1.2] - 2026-03-27

### Fixed
- Fix Dremio routing bug calling connect_postgresql instead of connect_dremio
- Add SPARQL injection escaping for pyoxigraph f-string queries
- Register atexit cleanup handler for session/store teardown
- Clean tmp/ subdirectories on startup, not just files
- Align execute_sql_query row limit from 10000 to 5000 (matching docstring)
- Fix GraphRAG init task tracking via session attribute instead of monkey-patch
- Remove circular import in chart handler
- Use typed exceptions in RDF handler
- Fix all failing tests (pytest shebang, FastMCP API changes, mock fixtures)

### Added
- Expose all 8 database drivers in connection handler (BigQuery, DuckDB, Databricks, MySQL)
- Extract _table_info_to_dict() utility for GraphRAG handler
- Update get_server_info to list all 8 databases, 28 tools, 10 features

### Changed
- Downgrade verbose schema analysis logs from INFO to DEBUG
- Remove unused ThreadPoolExecutor from DatabaseManager
- Remove stale Python 3.10-3.12 classifiers from pyproject.toml
- Remove outdated test_integration.py

## [1.1.1] - 2026-03-27

### Fixed
- Lowered `pandas` requirement from `>=3.0.0` to `>=2.2.3` to resolve dependency conflict with `databricks-sql-connector` (which caps pandas at `<2.4.0`)

### Changed
- Published to PyPI as `orionbelt-analytics`

## [1.1.0] - 2026-03-22

### Added
- **MySQL Support** - Full support for MySQL 8.0+ and MariaDB 10.5+
  - MySQL 5.7 reached EOL in October 2023 (no longer supported)
  - MySQL 8.0+ provides CTEs, window functions, and improved performance
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
