# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**OrionBelt Analytics** - A FastMCP server that analyzes database schemas (PostgreSQL, Snowflake, Dremio, ClickHouse) and generates RDF/OWL ontologies with embedded SQL mappings. Enables Text-to-SQL with fan-trap prevention and relationship-aware query construction. Includes GraphRAG for intelligent schema discovery via graph traversal and vector embeddings.

## Build & Development Commands

```bash
# Install dependencies (Python 3.13+ required)
uv sync

# Start MCP server (default: localhost:9000)
uv run server.py

# Run tests with coverage (pytest configured in pyproject.toml with --cov=src)
uv run pytest

# Run a single test file
uv run pytest tests/test_ontology_generator.py -v

# Run a specific test
uv run pytest tests/test_ontology_generator.py::test_function_name -v

# Code formatting
black src/ tests/
isort src/ tests/

# Linting and type checking
flake8 src/
mypy src/

# Run all pre-commit checks
pre-commit run --all-files

# Security scan
bandit -r src/
```

## Architecture

### Layered Design

The codebase follows a three-layer pattern:

1. **Registration layer** (`src/main.py`) - `@mcp.tool()` async decorators, thin wrappers only
2. **Handler layer** (`src/handlers/`) - Tool implementation logic, receives utilities as parameters
3. **Service layer** - Domain modules (`database_manager.py`, `ontology_generator.py`, etc.)

```
server.py → src/main.py (@mcp.tool decorators)
                ↓
            src/handlers/*.py (tool implementations)
                ↓
            src/database_manager.py, src/ontology_generator.py, etc.
```

### Core Modules

- **`src/main.py`** - FastMCP server setup, tool registration, resource/prompt definitions
- **`src/session.py`** - `SessionData` class for per-session state isolation with multi-schema support (per-schema ontology and GraphRAG via `SchemaState`)
- **`src/database_manager.py`** - Connection pooling, schema analysis, SQLAlchemy integration
- **`src/ontology_generator.py`** - RDF/OWL generation using rdflib with `oba:` (OrionBelt Analytics) namespace annotations
- **`src/oxigraph_store.py`** - Persistent RDF storage with SPARQL 1.1 query support
- **`src/obqc_validator.py`** - Ontology Basic Quality Criteria validation using sqlglot
- **`src/r2rml_generator.py`** - W3C R2RML mapping generation
- **`src/security.py`** - SQL injection prevention, fan-trap detection, credential encryption
- **`src/chart_utils.py`** - Plotly chart generation (kaleido for PNG export)
- **`src/paths.py`** - Centralized path resolution for output/skills/config directories

### Handler Modules (`src/handlers/`)

Each handler maps to a group of MCP tools:
- `connection.py` - `connect_database`, `list_schemas`
- `schema.py` - `discover_schema`, `get_table_details`, `reset_cache`
- `ontology.py` - `generate_ontology`, `suggest_semantic_names`, `apply_semantic_names`, `load_my_ontology`, `download_artifact`
- `query.py` - `execute_sql_query` (includes built-in validation), `sample_table_data`
- `chart.py` - `generate_chart`
- `rdf.py` - `query_sparql` (SELECT/ASK/CONSTRUCT), `store_ontology_in_rdf`, `add_rdf_knowledge`
- `graphrag.py` - `graphrag_search` (includes overview mode), `graphrag_query_context`, `graphrag_find_join_path`
- `workspace.py` - `cleanup_workspace`, `save_semantic_model`, `get_semantic_model`, `list_semantic_models`
- `info.py` - `get_server_info`

### Database Driver Pattern (`src/drivers/`)

Abstract base driver with per-database implementations handling dialect differences:
```
BaseDriver (src/drivers/base.py)
  ├─ PostgreSQLDriver
  ├─ SnowflakeDriver  (UPPERCASE identifiers, case-sensitive)
  ├─ DremioDriver
  └─ ClickHouseDriver (no FKs, system.* tables, ORDER BY defines sort)
```

### GraphRAG Subsystem (`src/graphrag/`)

Graph-based Retrieval Augmented Generation for schema intelligence:
- `manager.py` - Orchestrator, auto-initialized by `discover_schema()`
- `embedder.py` - Vector embeddings for schema elements
- `retriever.py` - Graph traversal and relationship discovery (up to 12 hops)
- `vector_store_chromadb.py` - ChromaDB vector storage implementation
- `community_detector.py` - Schema clustering via community detection

### Lifecycle Management (`src/lifecycle/`)

- `metadata.py` - Version tracking and retention policies for generated artifacts
- `cleanup.py` - Data cleanup manager for `tmp/` outputs

### Key Patterns

**Per-Session State Isolation**: Each MCP session maintains isolated `SessionData` (in `src/session.py`) with its own `DatabaseManager`, GraphRAG manager, and file paths, preventing cross-session interference. Ontology state is per-schema via `SchemaState` — switching schemas does not destroy the previous schema's ontology. GraphRAG and Oxigraph RDF store are connection-scoped and accumulative: each `discover_schema()` adds tables to the same graph, enabling cross-schema join path discovery and unified semantic search.

**Fan-Trap Prevention**: Multi-step validation prevents data multiplication errors:
1. `discover_schema()` extracts FK relationships
2. Pattern detection in `execute_sql_query()`
3. Suggests UNION ALL patterns for multi-fact aggregation

**Ontology Triple Storage**: RDF graphs link back to SQL:
- `ns:TableName` → OWL:Class with `oba:tableName`, `oba:primaryKey`
- `ns:relationship` → OWL:ObjectProperty with `oba:sqlJoinCondition`
- Persistent SPARQL queries via Oxigraph (`src/oxigraph_store.py`)

**MCP Resources**: Skills in `.claude/skills/` (fan-trap-prevention, sql-best-practices, chart-examples, analytical-workflow) are exposed as MCP resources via `@mcp.resource()` decorators in `main.py`.

## Configuration

Key environment variables (see `.env.template`):
- `MCP_TRANSPORT` - `http` (default) or `sse`
- `MCP_SERVER_PORT` - Server port (default: 9000)
- `ONTOLOGY_BASE_URI` - RDF base URI
- `R2RML_BASE_IRI` - R2RML mapping IRI
- `OUTPUT_DIR` - Generated files directory (default: `tmp`)
- Database configs: `POSTGRES_*`, `SNOWFLAKE_*`, `DREMIO_*`, `CLICKHOUSE_*`

## Code Standards

- Line length: 88 characters (black)
- Type hints required for all public functions (mypy strict)
- Google-style docstrings
- Conventional commits: `feat:`, `fix:`, `docs:`, `test:`, `refactor:`
- pytest with `asyncio_mode = "auto"` (configured in pyproject.toml)

## Implementation Guidelines

**IMPORTANT: Plan thoroughly before coding!**

When implementing changes, ALWAYS:

1. **Trace all code paths**: Before modifying a function, identify ALL places it's called from and ALL branches within it (success path, error path, fallback path, cache hit/miss, etc.)

2. **Consider database-specific behavior**: This codebase supports PostgreSQL, Snowflake, Dremio, and ClickHouse. Each has different:
   - Case sensitivity (Snowflake uses UPPERCASE identifiers; ClickHouse is case-sensitive)
   - Query syntax (SHOW commands, information_schema differences, system.* tables for ClickHouse)
   - Caching behavior (schema-level vs table-level queries)
   - Constraint model (ClickHouse has no FKs; PRIMARY KEY = sparse index, ORDER BY defines sort)

3. **Check cache implications**: When adding caching:
   - What happens on cache HIT?
   - What happens on cache MISS (fallback)?
   - Is the fallback path equally efficient?
   - Are cache keys consistent across related functions?

4. **Handle session state**: Changes to `SessionData` must consider:
   - When state is set (which tool sets it?)
   - When state is used (which tools depend on it?)
   - When state should be cleared (reconnect, new session?)

5. **Test edge cases**: Don't just test the happy path. Consider:
   - Empty results
   - Missing parameters (None/empty string)
   - Case sensitivity mismatches
   - Network/query failures

**Avoid incremental fixes** - solve problems completely the first time by understanding the full scope before writing code.

## Working Style

- **Verify before done**: Run tests and check logs to prove changes work before marking complete
- **Fix bugs directly**: When given a bug report, investigate and fix it - don't ask for hand-holding
- **Re-plan when stuck**: If implementation hits unexpected complexity, stop and re-plan rather than pushing through with hacky fixes
- **Challenge your work**: Before presenting a solution, ask "would a staff engineer approve this?"
