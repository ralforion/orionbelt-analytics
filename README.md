<!-- mcp-name: io.github.ralfbecher/orionbelt-analytics -->
<p align="center">
  <img src="https://raw.githubusercontent.com/ralfbecher/orionbelt-analytics/main/assets/ORIONBELT_Logo.png" alt="OrionBelt Logo" width="400">
</p>

<h1 align="center">OrionBelt Analytics</h1>

<p align="center"><strong>The Ontology-based MCP server for your Text-2-SQL convenience.</strong></p>

[![Version 1.3.0](https://img.shields.io/badge/version-1.3.0-purple.svg)](https://github.com/ralfbecher/orionbelt-analytics/releases)
[![Python 3.13+](https://img.shields.io/badge/python-3.13+-blue.svg)](https://www.python.org/downloads/)
[![License: BSL 1.1](https://img.shields.io/badge/License-BSL_1.1-orange.svg)](https://github.com/ralfbecher/orionbelt-analytics/blob/main/LICENSE)
[![FastMCP](https://img.shields.io/badge/FastMCP-3.2.4+-blue)](https://github.com/jlowin/fastmcp)
[![RDF/OWL](https://img.shields.io/badge/RDF%2FOWL-Ontology-orange)](https://www.w3.org/OWL/)

[![BigQuery](https://img.shields.io/badge/BigQuery-669DF6.svg?logo=googlebigquery&logoColor=white)](https://cloud.google.com/bigquery)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-4169E1.svg?logo=postgresql&logoColor=white)](https://www.postgresql.org)
[![Snowflake](https://img.shields.io/badge/Snowflake-29B5E8.svg?logo=snowflake&logoColor=white)](https://www.snowflake.com)
[![ClickHouse](https://img.shields.io/badge/ClickHouse-FFCC01.svg?logo=clickhouse&logoColor=black)](https://clickhouse.com)
[![Dremio](https://img.shields.io/badge/Dremio-31B48D.svg)](https://www.dremio.com)
[![Databricks](https://img.shields.io/badge/Databricks-FF3621.svg?logo=databricks&logoColor=white)](https://www.databricks.com)
[![DuckDB](https://img.shields.io/badge/DuckDB-FFF000.svg?logo=duckdb&logoColor=black)](https://duckdb.org)
[![MySQL](https://img.shields.io/badge/MySQL-4479A1.svg?logo=mysql&logoColor=white)](https://www.mysql.com)

OrionBelt Analytics is an MCP server that analyzes relational database schemas and generates RDF/OWL ontologies with embedded SQL mappings. It provides relationship-aware Text-to-SQL with automatic fan-trap prevention, GraphRAG for intelligent schema discovery, and interactive charting -- all accessible through any MCP-compatible AI client.

## The OrionBelt Ecosystem

| Project | Purpose |
|---------|---------|
| **OrionBelt Analytics** (this) | Schema analysis, ontology generation, GraphRAG, Text-to-SQL |
| [**OrionBelt Semantic Layer**](https://github.com/ralfbecher/orionbelt-semantic-layer) | Declarative YAML models compiled into dialect-specific, fan-trap-free SQL |
| [**OrionBelt Ontology Builder**](https://github.com/ralfbecher/orionbelt-ontology-builder) | Visual OWL ontology editor with reasoning and graph visualization ([live demo](https://orionbelt.streamlit.app/)) |
| [**OrionBelt Chat**](https://github.com/ralfbecher/orionbelt-chat) | AI chat UI for Analytics + Semantic Layer (Chainlit, multiple LLM providers) |

Run Analytics and Semantic Layer side-by-side in Claude Desktop for schema-aware ontology generation **and** guaranteed-correct SQL compilation.

## Architecture

<p align="center">
  <img src="https://raw.githubusercontent.com/ralfbecher/orionbelt-analytics/main/assets/architecture.png" alt="OrionBelt Analytics Architecture" width="900">
</p>

- **8 database connectors** -- PostgreSQL, MySQL, Snowflake, ClickHouse, Dremio, BigQuery, DuckDB/MotherDuck, Databricks SQL
- **RDF/OWL ontology generation** with `oba:` namespace SQL annotations and W3C R2RML mappings
- **GraphRAG** -- graph traversal (up to 12 hops) + ChromaDB vector embeddings for semantic schema discovery
- **SPARQL 1.1** query interface via persistent Oxigraph RDF store
- **Fan-trap prevention** -- automatic detection and safe query pattern suggestions
- **Interactive charting** -- Plotly charts with MCP-UI rendering in Claude Desktop
- **Workspace persistence** -- reconnect to the same database and restore your previous session

## Quick Start

### 1. Install

```bash
git clone https://github.com/ralfbecher/orionbelt-analytics
cd orionbelt-analytics
uv sync
```

> Requires **Python 3.13+** and [**uv**](https://github.com/astral-sh/uv).

### 2. Configure

```bash
cp .env.template .env
```

Edit `.env` with your database credentials. At minimum, set the variables for one database (e.g. `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DATABASE`, `POSTGRES_USERNAME`, `POSTGRES_PASSWORD`).

See [docs/configuration.md](docs/configuration.md) for all environment variables, transport options, and troubleshooting.

### 3. Run

```bash
uv run server.py
```

The server starts on `http://localhost:9000` (HTTP transport, configurable via `MCP_SERVER_PORT`).

## Connect Your AI Client

### Claude Desktop

Start the server, then add to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "OrionBelt-Analytics": {
      "command": "npx",
      "args": [
        "mcp-remote",
        "http://localhost:9000/mcp",
        "--transport",
        "http-only"
      ]
    }
  }
}
```

### Claude Code

```bash
claude mcp add orionbelt-analytics http://localhost:9000/mcp
```

### LibreChat

Set `MCP_TRANSPORT=sse` in `.env`, restart the server, then add to `librechat.yaml`:

```yaml
mcpServers:
  OrionBelt-Analytics:
    url: "http://host.docker.internal:9000/sse"
    timeout: 60000
    startup: true
```

### Other Frameworks

OrionBelt works with LangChain, OpenAI Agents SDK, CrewAI, Google ADK, Vercel AI SDK, n8n, and ChatGPT Custom GPTs. See [docs/integrations.md](docs/integrations.md) for setup examples.

## Tools

OrionBelt exposes 32 MCP tools. Here is a summary by category:

### Connection & Schema

| Tool | Description |
|------|-------------|
| `connect_database` | Connect to any supported database using `.env` credentials |
| `list_schemas` | List available schemas in the connected database |
| `reset_cache` | Clear cached schema and ontology data for the current session |
| `analyze_schema` | Analyze schema structure with automatic GraphRAG + ontology generation |
| `get_table_details` | Get detailed column, key, and constraint info for a specific table |
| `cleanup_workspace` | Delete all workspace files for the current connection and start fresh |

### Ontology & Semantic

| Tool | Description |
|------|-------------|
| `generate_ontology` | Generate RDF/OWL ontology from schema with SQL mapping annotations |
| `suggest_semantic_names` | Detect abbreviations and cryptic names for business-friendly renaming |
| `apply_semantic_names` | Apply LLM-suggested semantic names and descriptions to ontology |
| `load_my_ontology` | Load a custom `.ttl` ontology file from an import folder |
| `download_ontology` | Download the current ontology as a Turtle file |
| `download_r2rml` | Download the R2RML mapping as a Turtle file |

### Query & Visualization

| Tool | Description |
|------|-------------|
| `sample_table_data` | Preview table data with row limit and injection protection |
| `validate_sql_syntax` | Validate SQL with dialect detection, security analysis, fan-trap checks |
| `execute_sql_query` | Execute SQL with fan-trap detection and result validation |
| `generate_chart` | Generate Plotly charts (bar, line, scatter, heatmap) with MCP-UI rendering |

### GraphRAG

| Tool | Description |
|------|-------------|
| `initialize_graphrag` | Initialize graph + vector index for schema intelligence |
| `graphrag_search` | Semantic search across schema elements |
| `graphrag_query_context` | Get optimized context for SQL generation (85-95% token reduction) |
| `graphrag_find_join_path` | Discover join paths between tables via graph traversal |
| `graphrag_overview` | Schema statistics and community clustering overview |

### SPARQL & RDF

| Tool | Description |
|------|-------------|
| `store_ontology_in_rdf` | Persist ontology in Oxigraph for SPARQL access |
| `query_sparql` | Execute SPARQL SELECT queries |
| `query_sparql_ask` | Execute SPARQL ASK queries (true/false) |
| `list_tables_sparql` | List tables via SPARQL |
| `find_columns_by_type_sparql` | Find columns by data type via SPARQL |
| `add_rdf_knowledge` | Add custom metadata triples to the RDF store |
| `get_rdf_store_stats` | Get RDF store statistics |

### Semantic Models

| Tool | Description |
|------|-------------|
| `save_semantic_model` | Save a semantic model (e.g., OBML YAML) to the workspace |
| `get_semantic_model` | Retrieve a stored semantic model by name |
| `list_semantic_models` | List all stored semantic models for the current connection |

### System

| Tool | Description |
|------|-------------|
| `get_server_info` | Server version, features, and configuration |

For full parameter details, return values, and examples, see [docs/tools-reference.md](docs/tools-reference.md).

## Typical Workflows

**Full analysis session:**
```
connect_database("postgresql") -> analyze_schema("public") -> generate_ontology() -> execute_sql_query(...)
```

**Quick data exploration:**
```
connect_database("duckdb") -> list_schemas() -> sample_table_data("events")
```

**Query with visualization:**
```
validate_sql_syntax(query) -> execute_sql_query(query) -> generate_chart(data, "bar", ...)
```

**Resume a previous session (auto-restores workspace):**
```
connect_database("postgresql") -> execute_sql_query(...)
```

## Documentation

| Document | Contents |
|----------|----------|
| [Tools Reference](docs/tools-reference.md) | Full parameter docs, return values, and usage examples |
| [Configuration](docs/configuration.md) | Environment variables, transport setup, troubleshooting |
| [GraphRAG](docs/graphrag.md) | Graph-based schema intelligence and OBML workflow |
| [Fan-Trap Prevention](docs/fan-trap-prevention.md) | The fan-trap problem, detection, and safe SQL patterns |
| [Integrations](docs/integrations.md) | LangChain, OpenAI, CrewAI, Google ADK, Vercel, n8n, ChatGPT |
| [Development](docs/development.md) | Project structure, testing, contributing |

## License

Copyright 2025-2026 [RALFORION d.o.o.](https://ralforion.com)

Licensed under the Business Source License 1.1. See [LICENSE](LICENSE) for details.

**Change Date:** 2030-03-16 | **Change License:** Apache License, Version 2.0

For commercial licensing inquiries, contact: licensing@ralforion.com

---

<p align="center">
  <a href="https://ralforion.com">
    <img src="https://raw.githubusercontent.com/ralfbecher/orionbelt-analytics/main/assets/RALFORION_doo_Logo.png" alt="RALFORION d.o.o." width="200">
  </a>
</p>
