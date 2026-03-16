# OrionBelt Analytics v1.0.0

**First production release of OrionBelt Analytics**

**Release Date:** March 16, 2026
**Status:** Production/Stable
**License:** Business Source License 1.1 (converts to Apache 2.0 on 2030-03-16)

> **Note:** For Apache 2.0 licensing, use the `v0.7` branch.

---

## Highlights

**7 SQL Dialects** — PostgreSQL, Snowflake, ClickHouse, Dremio, BigQuery, DuckDB/MotherDuck, Databricks SQL

**New Database Support (v1.0)**
- **BigQuery** — Google Cloud data warehouse with service account authentication
- **DuckDB/MotherDuck** — Local OLAP files and cloud databases
- **Databricks SQL** — SQL Warehouse with Unity Catalog integration

**FastMCP 3.1+** — Upgraded MCP framework with enhanced streaming and error handling

**GraphRAG** — 12-hop graph traversal for intelligent schema discovery and relationship inference

**SPARQL Query Support** — 7 SPARQL tools for semantic querying of database ontologies via Oxigraph RDF store

**Production Hardening** — Security scanning (bandit, safety), credential encryption, SQL injection prevention

## Production Hardening

This release marks OrionBelt Analytics as **production-ready** with:

- **Security scanning tools** configured (bandit, safety)
- **Credential encryption** with AES-128-CBC + HMAC
- **SQL injection prevention** and identifier validation
- **Audit logging** for security events
- **Comprehensive test coverage** (12 test files)
- **Complete documentation** with troubleshooting guides for all 7 databases

## New Database Drivers

Three new production-ready drivers following the established `DatabaseDriver` protocol:

- **`src/drivers/bigquery.py`** (430 lines) — Project/dataset-level schema analysis
- **`src/drivers/duckdb.py`** (458 lines) — Local files, in-memory, and MotherDuck cloud
- **`src/drivers/databricks.py`** (512 lines) — Unity Catalog and Delta Lake support

Each driver provides:
- Schema introspection (get_schemas, get_tables, analyze_table)
- Query validation and execution
- Sample data retrieval
- Connection health checks
- Security validation and audit logging

## Configuration & Environment

**Complete `.env.template`** with configuration sections for all databases:
- BigQuery (project_id, dataset, credentials_path)
- DuckDB/MotherDuck (database_path, motherduck_token)
- Databricks SQL (server_hostname, http_path, access_token, catalog)

**Database-specific troubleshooting guides** covering:
- Authentication and credential management
- Connection parameters and network configuration
- Common errors and solutions

## Dependencies

### New
```toml
sqlalchemy-bigquery>=1.11.0     # BigQuery support
duckdb>=1.1.0                   # DuckDB engine
duckdb-engine>=0.13.0           # DuckDB SQLAlchemy dialect
databricks-sql-connector>=3.5.0 # Databricks SQL support
```

### Updated
```toml
fastmcp>=3.1.0  # (was >=3.0.2) - Latest MCP protocol features
```

## Compatibility

**OrionBelt Platform Integration** — Fully compatible with:
- OrionBelt Semantic Layer v1.0.0
- OrionBelt Semantic Layer MCP v1.0.0

All three components now support the same 7 database vendors, enabling seamless workflows from schema discovery (Analytics) to semantic modeling (Semantic Layer) to SQL compilation.

## Migration

**No breaking changes from v0.7.0** — All existing functionality preserved. New database support is additive.

**Upgrading:**
```bash
cd orionbelt-analytics
git pull origin main
uv sync
uv run server.py
```

## Installation

```bash
git clone https://github.com/ralfbecher/orionbelt-analytics
cd orionbelt-analytics
uv sync
cp .env.template .env
# Edit .env with your database credentials
uv run server.py
```

## Quick Start Examples

### BigQuery
```python
from src.database_manager import DatabaseManager

db_manager = DatabaseManager()
success = db_manager.connect_bigquery(
    project_id='your-gcp-project',
    dataset='your_dataset',
    credentials_path='/path/to/service-account-key.json'
)
```

### DuckDB
```python
db_manager = DatabaseManager()
success = db_manager.connect_duckdb(
    database_path='/data/analytics.db'  # or ':memory:'
)
```

### Databricks SQL
```python
db_manager = DatabaseManager()
success = db_manager.connect_databricks(
    server_hostname='your-workspace.cloud.databricks.com',
    http_path='/sql/1.0/warehouses/abc123',
    access_token='your_token',
    catalog='hive_metastore',
    schema='default'
)
```

## Documentation

- [README](README.md) — Complete documentation with examples
- [CHANGELOG](CHANGELOG.md) — Detailed change history
- [CONTRIBUTING](CONTRIBUTING.md) — Contribution guidelines
- [CLA](CLA.md) — Contributor License Agreement

## License

Copyright 2025-2026 [RALFORION d.o.o.](https://ralforion.com)

Licensed under the **Business Source License 1.1** (BSL 1.1).

- **Change Date:** 2030-03-16
- **Change License:** Apache License, Version 2.0
- **Commercial Licensing:** Contact licensing@ralforion.com

See [LICENSE](LICENSE) for full details.

> **Note:** For Apache 2.0 licensing, use the `v0.7` branch.

## Support

- **Issues:** https://github.com/ralfbecher/orionbelt-analytics/issues
- **Discussions:** https://github.com/ralfbecher/orionbelt-analytics/discussions

---

**OrionBelt Analytics v1.0.0** — Production-ready database schema analysis and ontology generation for AI-powered analytics across 7 major database platforms.
