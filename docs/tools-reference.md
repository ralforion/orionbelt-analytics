[<- Back to README](../README.md)

# MCP Tools Reference

Complete reference for all OrionBelt Analytics MCP tools. These tools are invoked by AI clients (Claude, etc.) through the Model Context Protocol -- they are not Python functions.

---

## Recommended Workflows

### Standard Analysis Workflow

1. **connect_database** -- establish a secure database connection
2. **list_schemas** -- discover available schemas
3. **analyze_schema** -- extract schema structure with relationships (auto-generates R2RML)
4. **generate_ontology** -- create semantic ontology with `oba:` annotations
5. **suggest_semantic_names** -- identify cryptic/abbreviated names for review
6. **apply_semantic_names** -- apply LLM-suggested improvements
7. **execute_sql_query** -- run validated SQL with fan-trap protection
8. **generate_chart** -- visualize results

### Resuming a Previous Session

1. **connect_database** -- reconnect to the same database
2. **restore_workspace** -- reload schema cache, ontology, GraphRAG, and RDF store from disk
3. Continue with **execute_sql_query**, **generate_chart**, etc.

### Quick Data Exploration

1. **connect_database** -- connect
2. **analyze_schema** -- lightweight mode (default) for fast overview
3. **sample_table_data** -- preview actual data
4. **execute_sql_query** -- run queries

---

## Tool Reference

### 1. connect_database

Connect to a database using credentials from environment variables.

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `db_type` | string | Yes | Database type: `postgresql`, `mysql`, `snowflake`, `clickhouse`, `dremio`, `bigquery`, `duckdb`, `databricks` |

**Returns:** Connection status message. If a previous workspace exists for this connection, includes a workspace summary with available artifacts.

**Key Features:**
- Credentials are read from environment variables (e.g., `POSTGRES_HOST`, `SNOWFLAKE_ACCOUNT`), not passed as parameters
- Automatically detects existing workspaces from prior sessions
- Clears session state when switching to a different database connection
- Generates a connection fingerprint for workspace scoping

**Environment Variables by Database:**

| Database | Required Variables |
|----------|-------------------|
| PostgreSQL | `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DATABASE`, `POSTGRES_USERNAME`, `POSTGRES_PASSWORD` |
| MySQL | `MYSQL_HOST`, `MYSQL_DATABASE`, `MYSQL_USERNAME`, `MYSQL_PASSWORD` |
| Snowflake | `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_USERNAME`, `SNOWFLAKE_PASSWORD`, `SNOWFLAKE_WAREHOUSE`, `SNOWFLAKE_DATABASE` |
| ClickHouse | `CLICKHOUSE_HOST`, `CLICKHOUSE_DATABASE` |
| Dremio | `DREMIO_URI` + `DREMIO_PAT` (preferred), or `DREMIO_HOST` + `DREMIO_PORT` + `DREMIO_USERNAME` + `DREMIO_PASSWORD` |
| BigQuery | `BIGQUERY_PROJECT_ID` |
| DuckDB | None required (defaults to in-memory); optional: `DUCKDB_DATABASE_PATH`, `MOTHERDUCK_TOKEN` |
| Databricks | `DATABRICKS_SERVER_HOSTNAME`, `DATABRICKS_HTTP_PATH`, `DATABRICKS_ACCESS_TOKEN` |

---

### 2. list_schemas

List available schemas from the connected database.

**Parameters:** None

**Returns:** Array of schema name strings.

**Key Features:**
- Requires `connect_database` to be called first
- Useful for multi-schema databases to identify which schema to analyze

---

### 3. reset_cache

Clear cached schema and/or ontology data to force re-analysis.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `cache_type` | string | No | `"all"` | Type of cache to reset: `"schema"`, `"ontology"`, or `"all"` |

**Returns:** Dictionary with status and list of cleared cache types.

**Key Features:**
- Resetting `schema` clears cached table metadata, schema file, and R2RML file references
- Resetting `ontology` clears the ontology file, loaded ontology content, and OBQC validator
- Use this when the database schema has changed and you need fresh analysis

---

### 4. analyze_schema

Analyze database schema and return table metadata with relationships. Automatically generates W3C R2RML mappings and triggers GraphRAG initialization in the background.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `schema_name` | string | No | Default schema | Schema to analyze |
| `lightweight` | boolean | No | `true` | If true, return minimal data (table names, FK relationships, fan-trap warnings). If false, return full schema with all column details. |

**Returns:** Dictionary containing:
- `table_count` -- number of tables found
- `tables` -- table summaries (lightweight) or full details
- `relationships` -- foreign key relationships between tables
- `fan_trap_warnings` -- tables with multiple FK relationships (potential data multiplication risk)
- `schema_file` -- path to saved schema JSON (full mode)
- `r2rml_file` -- path to generated R2RML mapping (full mode)
- `next_step` -- recommended next tool to call

**Key Features:**
- Requires `connect_database` first
- Results are cached for the session -- calling again returns cached data immediately
- Lightweight mode (default) saves significant tokens by returning only table names and relationships
- Use `get_table_details` to drill into specific tables after lightweight analysis
- Automatically generates R2RML mappings in full mode
- Auto-initializes GraphRAG in the background (configurable via `AUTO_GRAPHRAG` env var)
- Detects fan-trap risks: tables connecting to multiple other tables via foreign keys

---

### 5. get_table_details

Get detailed metadata for a single table, including all columns, data types, keys, and constraints.

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `table_name` | string | Yes | Name of the table to analyze |
| `schema_name` | string | No | Schema containing the table |

**Returns:** Dictionary containing:
- `columns` -- array of column details (name, data type, nullability, key status, comments)
- `primary_keys` -- list of primary key columns
- `foreign_keys` -- list of foreign key relationships
- `row_count` -- approximate row count
- `comment` -- table-level comment if available

**Key Features:**
- Requires `connect_database` first
- Ideal companion to lightweight `analyze_schema` -- get full details for specific tables only
- Returns foreign key targets with referenced table and column names

---

### 6. generate_ontology

Generate an RDF/OWL ontology from the database schema with `oba:` (OrionBelt Analytics) namespace annotations that link ontology classes directly to SQL tables and columns.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `schema_name` | string | No | Last analyzed schema | Schema to generate ontology for |
| `schema_info` | string | No | None | Pre-analyzed schema JSON (usually not needed -- uses cached schema automatically) |
| `base_uri` | string | No | `"http://example.com/ontology/"` | Base URI for the ontology namespace |
| `auto_persist` | boolean | No | `true` | Automatically store in Oxigraph RDF database |
| `graph_uri` | string | No | Auto-generated | Custom named graph URI for RDF storage |

**Returns:** Status message with ontology file path, table count, and (if auto-persisted) triple count and graph URI.

**Key Features:**
- Automatically uses cached schema from `analyze_schema` -- no need to pass schema data
- Returns cached result if ontology was already generated this session
- Generates OWL classes for tables with `oba:tableName`, `oba:primaryKey` annotations
- Generates OWL ObjectProperties for relationships with `oba:sqlJoinCondition`
- Generates OWL DatatypeProperties for columns with `oba:columnName`, `oba:sqlDataType`
- Auto-persists to Oxigraph RDF store for SPARQL querying (when `auto_persist` is true)
- Analyzes generated names and reports how many may need semantic review
- Saves ontology as `.ttl` (Turtle) file in connection-scoped output directory

---

### 7. suggest_semantic_names

Extract and analyze names from a generated ontology to identify abbreviations, cryptic identifiers, and names that would benefit from human-readable alternatives.

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `ontology_file` | string | No | Ontology filename (auto-detected from session if not provided) |

**Returns:** Dictionary containing:
- `classes_for_review` -- table-level names that appear cryptic
- `properties_for_review` -- column-level names needing improvement
- `relationships_for_review` -- relationship names to clarify
- `summary` -- counts of items needing review
- `instructions` -- guidance on how to provide better names

**Key Features:**
- Uses the cached ontology from `generate_ontology` automatically
- Identifies abbreviated names (e.g., `acctbal` -> `AccountBalance`)
- Provides the foundation for the `apply_semantic_names` step
- Does not modify the ontology -- only extracts names for review

---

### 8. apply_semantic_names

Apply LLM-suggested semantic name improvements to an existing ontology, replacing cryptic identifiers with business-friendly labels.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `suggestions` | string | Yes | -- | JSON object with `classes`, `properties`, and/or `relationships` arrays |
| `ontology_file` | string | No | Auto-detected | Ontology filename to update |
| `save_to_file` | boolean | No | `true` | Whether to save the updated ontology |

The `suggestions` parameter expects a JSON structure like:

```json
{
  "classes": [
    {
      "original_name": "acctbal",
      "suggested_name": "AccountBalance",
      "description": "Account balance records"
    }
  ],
  "properties": [
    {
      "original_name": "bankid",
      "table_name": "acctbal",
      "suggested_name": "Bank Identifier"
    }
  ],
  "relationships": [
    {
      "original_name": "acctbal_to_banks",
      "suggested_name": "Account Bank Relationship"
    }
  ]
}
```

**Returns:** Status message confirming applied changes.

**Key Features:**
- Updates `rdfs:label` annotations on OWL classes, properties, and relationships
- Optionally re-persists updated ontology to Oxigraph RDF store
- Saves updated `.ttl` file to the connection-scoped output directory

---

### 9. load_my_ontology

Load a custom `.ttl` (Turtle) ontology file from the import folder, bypassing the automated generation pipeline.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `import_folder` | string | No | `"./import"` | Path to the folder containing `.ttl` files |
| `auto_persist` | boolean | No | `true` | Store in Oxigraph RDF database |
| `graph_uri` | string | No | Auto-generated | Custom graph URI for RDF storage |

**Returns:** Dictionary with ontology information including class count, property count, and storage status.

**Key Features:**
- Automatically selects the newest `.ttl` file in the import folder
- Enables OBQC (Ontology Basic Quality Criteria) validation for subsequent SQL queries
- Useful for loading externally curated or hand-crafted ontologies
- Supports the same auto-persist workflow as `generate_ontology`

---

### 10. sample_table_data

Safely sample rows from a specific table for data exploration and quality assessment.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `table_name` | string | Yes | -- | Name of the table to sample |
| `schema_name` | string | No | Default schema | Schema containing the table |
| `limit` | integer | No | `10` | Maximum rows to return (max: 100) |

**Returns:** Array of row dictionaries.

**Key Features:**
- Requires `connect_database` first
- Enforces a maximum of 100 rows for safety
- Invalid or out-of-range limits are silently corrected to 10
- Useful for understanding data format before writing queries

---

### 11. validate_sql_syntax

Validate SQL syntax, security, and fan-trap risks before execution. Performs both structural validation and ontology-aware semantic checks (OBQC).

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `sql_query` | string | Yes | SQL SELECT statement to validate |

**Returns:** Dictionary containing:
- `is_valid` -- boolean validation result
- `error` -- error message if invalid
- `error_type` -- category of error (`parameter_error`, `connection_error`, `obqc_error`, etc.)
- `warnings` -- array of warning messages
- `suggestions` -- array of improvement suggestions
- `database_dialect` -- detected SQL dialect
- `obqc_valid` -- ontology-based validation result (if ontology is loaded)
- `obqc_issues` -- detailed OBQC validation issues
- `fan_trap_risk` -- whether the query risks data multiplication
- `next_tool` -- recommended next tool (`execute_sql_query` if valid)

**Key Features:**
- Checks SQL injection patterns and blocks dangerous statements
- Validates against the database dialect of the current connection
- OBQC validation runs automatically when an ontology is loaded (via `generate_ontology` or `load_my_ontology`)
- Detects fan-trap risks: queries aggregating across multiple 1:many relationships
- Suggests UNION ALL patterns for multi-fact aggregation scenarios

---

### 12. execute_sql_query

Execute a SQL query with built-in validation, fan-trap protection, and automatic GraphRAG context enrichment.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `sql_query` | string | Yes | -- | SQL SELECT statement with fully qualified identifiers (`schema.table.column`) |
| `limit` | integer | No | `1000` | Maximum rows to return (max: 5,000) |
| `checklist_completed` | boolean | No | `false` | Confirmation that the pre-execution checklist has been completed |
| `query_intent` | string | No | Auto-extracted | Natural language description of what the query aims to retrieve |

**Returns:** Dictionary containing:
- `success` -- boolean execution result
- `columns` -- array of column names
- `rows` -- array of result rows
- `row_count` -- number of rows returned
- `execution_time_ms` -- query execution time in milliseconds
- `next_tool` -- suggests `generate_chart` when results contain data

**Key Features:**
- Requires `connect_database` first
- **Pre-execution checklist must be confirmed** (`checklist_completed: true`) or the query is rejected
- Read-only enforcement -- only SELECT statements and schema introspection queries are allowed
- SQL injection prevention
- Query timeout protection
- Result size capped at 5,000 rows
- Automatically retrieves GraphRAG context for relevant tables when available
- Fan-trap detection warns about data multiplication in multi-table joins
- `query_intent` enables better GraphRAG context retrieval; if omitted, intent is auto-extracted from the SQL

---

### 13. generate_chart

Generate interactive Plotly charts rendered via MCP Apps, or export as static PNG images.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `data_source` | array | Yes | -- | JSON array of objects, e.g., `[{"name": "A", "value": 10}]`. Pass as an array, not a string. |
| `chart_type` | string | Yes | -- | Chart type: `bar`, `line`, `scatter`, or `heatmap` |
| `x_column` | string | Yes | -- | Column name for the X-axis |
| `y_column` | string or array | No | None | Column name(s) for the Y-axis. Pass an array for multi-series charts. |
| `color_column` | string | No | None | Column for grouping/coloring. For heatmaps: the numeric value column for color intensity. |
| `title` | string | No | None | Chart title |
| `chart_style` | string | No | `"grouped"` | Layout style: `default`, `stacked`, or `grouped` |
| `width` | integer | No | `800` | Chart width in pixels |
| `height` | integer | No | `600` | Chart height in pixels |
| `sort_by` | string | No | None | Column to sort by |
| `sort_order` | string | No | None | Sort direction: `ascending` or `descending` |
| `output_format` | string | No | `"interactive"` | `"interactive"` (renders via MCP Apps) or `"image"` (saves PNG file) |

**Returns:**
- Interactive mode: `"Chart generated: ui://orionbelt/chart/<uuid>"` -- the chart is registered as a dynamic MCP Apps resource
- Image mode: `"Chart saved to: <file_path>"` -- path to the saved PNG file

**Key Features:**
- Interactive charts are rendered via FastMCP Apps as self-contained HTML with Plotly.js
- PNG export uses Kaleido for server-side rendering
- Supports multi-series Y-axis by passing an array of column names
- Heatmap charts use `x_column` for X-axis, `y_column` for Y-axis, and `color_column` for cell values
- Heatmap axes are sorted by ordinal order for weekdays and time-of-day categories
- Charts are saved to the connection-scoped output directory

---

### 14. restore_workspace

Restore a workspace from a previous session's artifacts, avoiding the cost of re-analyzing the schema and regenerating the ontology.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `schema_name` | string | No | Auto-selects first available | Schema to restore |

**Returns:** Markdown-formatted summary listing what was restored and what is ready to use.

**Key Features:**
- Requires `connect_database` first (same database as the previous session)
- Restores up to four components from disk:
  - **Schema cache** -- cached table metadata from `analyze_schema`
  - **Ontology** -- generated or loaded `.ttl` ontology file (tracks enrichment state)
  - **RDF store** -- Oxigraph persistent SPARQL database
  - **GraphRAG** -- graph index and vector embeddings
- Lists available **semantic models** stored via `save_semantic_model`
- If multiple schemas exist in the workspace, auto-selects the first one and reports alternatives
- Reports "DO NOT CALL" tools (already restored) and "Ready to Use" tools
- After restoration, tools like `validate_sql_syntax`, `graphrag_search`, and `execute_sql_query` work immediately without re-running the analysis pipeline
- Workspace data persists across server restarts (chart images are cleaned on startup)

---

### 15. save_semantic_model

Save a semantic model definition (e.g., OBML YAML) to the workspace for reuse across sessions.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `model_yaml` | string | Yes | -- | The model definition in YAML format |
| `model_name` | string | Yes | -- | Name to identify this model (e.g., "sales_analytics") |
| `schema_name` | string | No | Auto-detected | Database schema this model is based on |

**Returns:** Dictionary with `success`, `model_name`, `schema_name`, `file`, and `message`.

**Key Features:**
- Stores model YAML in `tmp/{connection_id}/models/{name}.yaml`
- Tracks models in workspace metadata for `restore_workspace` discovery
- Model content is treated as opaque -- no parsing or validation of the YAML structure
- Enables cross-session model persistence for use with external Semantic Layer tools

---

### 16. get_semantic_model

Retrieve a stored semantic model YAML by name.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `model_name` | string | Yes | -- | Name of the model to retrieve |

**Returns:** Dictionary with `success`, `model_name`, `schema_name`, `saved_at`, and `model_yaml`.

**Key Features:**
- Returns the full YAML content of a previously saved model
- Use this to pass model content to a Semantic Layer's `load_model` tool

---

### 17. list_semantic_models

List all stored semantic models for the current database connection.

**Parameters:** None

**Returns:** Dictionary with `models` array (each entry has `model_name`, `schema_name`, `saved_at`) and `count`.

---

### 18. get_server_info

Retrieve server metadata, capabilities, and the full list of available tools.

**Parameters:** None

**Returns:** Dictionary containing:
- `name` -- server name
- `version` -- server version
- `description` -- server description
- `supported_databases` -- array of supported database types
- `features` -- array of feature descriptions
- `tools` -- array of all registered tool names
- `next_tool` -- suggests `connect_database` as the starting point

**Key Features:**
- Good starting point for clients to discover server capabilities
- Lists all supported databases and tools in a single response

---

## Security Model

All tools operate within these security constraints:

- **Read-only SQL** -- only SELECT statements and schema introspection queries are permitted
- **SQL injection prevention** -- queries are scanned for injection patterns before execution
- **Query timeout protection** -- long-running queries are terminated
- **Result size limits** -- maximum 5,000 rows per query
- **Credential isolation** -- database credentials are read from environment variables, never passed as tool parameters
- **Session isolation** -- each MCP session maintains independent state (connections, caches, artifacts)
- **Idle session eviction** -- sessions are automatically cleaned up after a configurable idle timeout
