# OrionBelt Analytics MCP Tools Reference

Complete documentation of all 28 MCP tools with parameters, return types, and usage examples.

**Version:** 0.5.0
**Last Updated:** 2026-02-27

---

## Table of Contents

- [Database Connection Tools](#database-connection-tools)
- [Schema Analysis Tools](#schema-analysis-tools)
- [Ontology Generation Tools](#ontology-generation-tools)
- [SQL Query Tools](#sql-query-tools)
- [Visualization Tools](#visualization-tools)
- [GraphRAG Tools](#graphrag-tools)
- [RDF Store & SPARQL Tools](#rdf-store--sparql-tools)
- [Common Workflows](#common-workflows)

---

## Database Connection Tools

### 1. `connect_database`

**Description:** Connect to a database (PostgreSQL, Snowflake, Dremio, or ClickHouse).

**Input Parameters:**
- `db_type` (str, required): Database type - 'postgresql', 'snowflake', 'dremio', or 'clickhouse'
- `host` (str, optional): Database host (uses .env if not provided)
- `port` (int, optional): Database port (uses .env if not provided)
- `database` (str, optional): Database name (uses .env if not provided)
- `username` (str, optional): Username (uses .env if not provided)
- `password` (str, optional): Password (uses .env if not provided)
- `account` (str, optional): Snowflake account identifier
- `warehouse` (str, optional): Snowflake warehouse
- `schema` (str, optional): Snowflake schema (default: "PUBLIC")
- `role` (str, optional): Snowflake role
- `ssl` (bool, optional): Enable SSL for Dremio (default: False)
- `uri` (str, optional): Dremio URI for PAT-based authentication
- `pat` (str, optional): Dremio Personal Access Token

**Returns:**
```json
{
  "success": true,
  "message": "Successfully connected to postgresql database",
  "connection_info": {
    "database_type": "postgresql",
    "host": "localhost",
    "port": 5432,
    "database": "mydb"
  }
}
```

**Usage Example:**
```python
# Using environment variables
connect_database(db_type="postgresql")

# Override specific parameters
connect_database(db_type="postgresql", host="custom.host.com", port=5433)
```

---

### 2. `list_schemas`

**Description:** Get a list of available schemas from the connected database.

**Input Parameters:** None

**Returns:**
```json
["public", "information_schema", "pg_catalog", "myschema"]
```

**Usage Example:**
```python
list_schemas()
```

---

### 3. `get_server_info`

**Description:** Get comprehensive information about the MCP server and its capabilities.

**Input Parameters:** None

**Returns:**
```json
{
  "server_name": "OrionBelt Analytics",
  "version": "0.5.0",
  "description": "AI-Powered Database Intelligence with GraphRAG...",
  "supported_databases": ["PostgreSQL", "Snowflake", "Dremio", "ClickHouse"],
  "features": [...],
  "tools": [...],
  "graphrag_available": true,
  "oxigraph_available": true
}
```

**Usage Example:**
```python
get_server_info()
```

---

## Schema Analysis Tools

### 4. `analyze_schema`

**Description:** Analyze database schema and return table metadata with relationships. Supports lightweight mode for 90% token reduction.

**Input Parameters:**
- `schema_name` (str, optional): Schema to analyze (uses default if not specified)
- `lightweight` (bool, optional): If True (default), return minimal data; if False, return full schema

**Returns (Lightweight Mode):**
```json
{
  "schema": "public",
  "table_count": 25,
  "table_names": ["customers", "orders", "products", ...],
  "relationships": [
    {
      "from_table": "orders",
      "from_column": "customer_id",
      "to_table": "customers",
      "to_column": "id"
    }
  ],
  "fan_trap_warnings": [...],
  "next_steps": [...]
}
```

**Returns (Full Mode):**
```json
{
  "schema": "public",
  "table_count": 25,
  "tables": [
    {
      "name": "customers",
      "columns": [...],
      "primary_keys": [...],
      "foreign_keys": [...]
    }
  ],
  "schema_file": "tmp/schema_public_20260227.json",
  "r2rml_file": "tmp/r2rml_public_20260227.ttl"
}
```

**Token Savings:** Lightweight mode: ~5-10k tokens vs Full mode: ~36-145k tokens (90% reduction)

**Usage Example:**
```python
# Lightweight analysis (recommended)
analyze_schema(schema_name="public", lightweight=True)

# Full analysis (when you need all column details)
analyze_schema(schema_name="public", lightweight=False)
```

---

### 5. `get_table_details`

**Description:** Get detailed metadata for a single table on-demand.

**Input Parameters:**
- `table_name` (str, required): Name of the table to analyze
- `schema_name` (str, optional): Schema containing the table

**Returns:**
```json
{
  "name": "customers",
  "schema": "public",
  "columns": [
    {
      "name": "id",
      "data_type": "INTEGER",
      "nullable": false,
      "default": null
    }
  ],
  "primary_keys": ["id"],
  "foreign_keys": [],
  "row_count": 15234,
  "comment": null
}
```

**Usage Example:**
```python
# After lightweight schema analysis, get details for specific tables
get_table_details(table_name="customers", schema_name="public")
```

---

### 6. `sample_table_data`

**Description:** Sample data from a specific table for analysis and exploration.

**Input Parameters:**
- `table_name` (str, required): Name of the table to sample
- `schema_name` (str, optional): Schema containing the table
- `limit` (int, optional): Maximum rows to return (default: 10, max: 100)

**Returns:**
```json
[
  {
    "id": 1,
    "name": "John Doe",
    "email": "john@example.com",
    "created_at": "2024-01-15"
  },
  {
    "id": 2,
    "name": "Jane Smith",
    "email": "jane@example.com",
    "created_at": "2024-01-16"
  }
]
```

**Usage Example:**
```python
sample_table_data(table_name="customers", schema_name="public", limit=50)
```

---

### 7. `reset_cache`

**Description:** Reset cached schema and/or ontology data to force re-analysis.

**Input Parameters:**
- `cache_type` (str, optional): "schema", "ontology", "all", or None (default: all)

**Returns:**
```json
{
  "status": "success",
  "cleared_caches": ["schema", "ontology"],
  "message": "Cache cleared successfully",
  "next_steps": [...]
}
```

**Usage Example:**
```python
# Clear all caches
reset_cache()

# Clear only schema cache
reset_cache(cache_type="schema")
```

---

## Ontology Generation Tools

### 8. `generate_ontology`

**Description:** Generate RDF/OWL ontology from database schema. AUTO-ANALYZES schema if not cached!

**Input Parameters:**
- `schema_info` (str, optional): Optional pre-analyzed schema JSON
- `schema_name` (str, optional): Schema name to analyze and generate ontology for
- `base_uri` (str, optional): Base URI for ontology (default: "http://example.com/ontology/")
- `auto_persist` (bool, optional): Store in Oxigraph RDF database (default: True)
- `graph_uri` (str, optional): Custom graph URI for RDF storage

**Returns (auto_persist=True):**
```
Ontology generated and stored in RDF store successfully!

📊 Statistics:
- Graph URI: http://example.com/ontology/public
- Total triples: 1,247
- Tables: 25
- Columns: 312
- Relationships: 18

✅ Ontology is now queryable via SPARQL tools
```

**Returns (auto_persist=False):**
```turtle
@prefix db: <http://example.com/ontology/> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .

# Full RDF ontology in Turtle format (23k-94k tokens)
```

**Token Savings:** auto_persist=True saves 23k-94k tokens

**Usage Example:**
```python
# Default: auto-persist to RDF store (recommended)
generate_ontology(schema_name="public")

# Get full Turtle output
generate_ontology(schema_name="public", auto_persist=False)
```

---

### 9. `suggest_semantic_names`

**Description:** Extract and analyze names from ontology to identify abbreviations and cryptic names for business-friendly improvements.

**Input Parameters:**
- `ontology_file` (str, optional): Ontology filename from generate_ontology (e.g., "ontology_TPCDS.ttl")

**Returns:**
```json
{
  "classes": [
    {
      "original_name": "cust_mstr",
      "technical_name": "cust_mstr",
      "issues": ["abbreviation", "technical_suffix"],
      "suggestions_needed": true
    }
  ],
  "properties": [
    {
      "original_name": "ord_dt",
      "table_name": "orders",
      "technical_name": "ord_dt",
      "issues": ["abbreviation", "cryptic_suffix"]
    }
  ],
  "relationships": [...],
  "summary": {
    "total_classes": 25,
    "classes_needing_suggestions": 18,
    "total_properties": 312,
    "properties_needing_suggestions": 156
  },
  "llm_instructions": "..."
}
```

**Usage Example:**
```python
suggest_semantic_names(ontology_file="ontology_public.ttl")
```

---

### 10. `apply_semantic_names`

**Description:** Apply LLM-suggested semantic names to an existing ontology.

**Input Parameters:**
- `suggestions` (str, required): JSON string with name suggestions
- `ontology_file` (str, optional): Ontology filename
- `save_to_file` (bool, optional): Whether to save updated ontology (default: True)

**Suggestions Format:**
```json
{
  "classes": [
    {
      "original_name": "cust_mstr",
      "suggested_name": "Customer Master",
      "description": "Main customer entity with contact information"
    }
  ],
  "properties": [
    {
      "original_name": "ord_dt",
      "table_name": "orders",
      "suggested_name": "Order Date",
      "description": "Date when order was placed"
    }
  ],
  "relationships": [...]
}
```

**Returns:**
```
Semantic names applied successfully!

📝 Summary:
- Classes updated: 18
- Properties updated: 156
- Relationships updated: 12

💾 Updated ontology saved to: tmp/ontology_public_semantic_20260227.ttl

✨ What changed:
- rdfs:label → Business-friendly name
- db:semanticName → New annotation
- rdfs:comment → Description
- Original db:tableName/db:columnName preserved for SQL
```

**Usage Example:**
```python
suggestions = '''
{
  "classes": [
    {"original_name": "cust", "suggested_name": "Customer", "description": "..."}
  ]
}
'''
apply_semantic_names(suggestions=suggestions, ontology_file="ontology_public.ttl")
```

---

### 11. `load_my_ontology`

**Description:** Load the newest .ttl ontology file from the import folder instead of generating from database.

**Input Parameters:**
- `import_folder` (str, optional): Path to folder with .ttl files (default: "./import")
- `auto_persist` (bool, optional): Store in Oxigraph RDF database (default: True)
- `graph_uri` (str, optional): Custom graph URI for RDF storage

**Returns:**
```json
{
  "success": true,
  "file_path": "./import/my_ontology.ttl",
  "file_name": "my_ontology.ttl",
  "file_size": "245 KB",
  "modified_time": "2026-02-26 15:30:00",
  "classes_count": 25,
  "properties_count": 312,
  "relationships_count": 18,
  "stored_in_rdf": true,
  "graph_uri": "http://example.com/ontology/my_ontology",
  "next_steps": [...]
}
```

**Usage Example:**
```python
# Load from default ./import folder
load_my_ontology()

# Load from custom folder
load_my_ontology(import_folder="/path/to/ontologies")
```

---

### 12. `download_ontology`

**Description:** Download ontology as TTL file from RDF store or tmp folder.

**Input Parameters:**
- `schema_name` (str, optional): Schema name
- `source` (str, optional): "rdf" (from Oxigraph) or "file" (from tmp folder, default)

**Returns:**
```json
{
  "success": true,
  "content": "@prefix db: <http://...> .\n\n# Full Turtle content",
  "file_path": "tmp/ontology_public_20260227.ttl",
  "file_name": "ontology_public_20260227.ttl",
  "file_size": "156 KB",
  "triple_count": 1247,
  "source": "rdf"
}
```

**Usage Example:**
```python
# Download from RDF store
download_ontology(schema_name="public", source="rdf")

# Download from tmp folder
download_ontology(schema_name="public", source="file")
```

---

### 13. `download_r2rml`

**Description:** Download R2RML (RDB to RDF Mapping Language) file automatically generated by analyze_schema.

**Input Parameters:**
- `schema_name` (str, optional): Schema name

**Returns:**
```json
{
  "success": true,
  "content": "@prefix rr: <http://...> .\n\n# Full R2RML mapping",
  "file_path": "tmp/r2rml_public_20260227.ttl",
  "file_name": "r2rml_public_20260227.ttl",
  "file_size": "89 KB",
  "base_iri": "http://mycompany.com/",
  "schema_name": "public",
  "usage_examples": [...]
}
```

**Usage Example:**
```python
download_r2rml(schema_name="public")
```

---

## SQL Query Tools

### 14. `validate_sql_syntax`

**Description:** Validate SQL syntax, security, and fan-trap risks before execution.

**Input Parameters:**
- `sql_query` (str, required): SQL SELECT statement to validate

**Returns:**
```json
{
  "is_valid": true,
  "warnings": [
    "Query joins multiple fact tables (orders, shipments) - potential fan-trap"
  ],
  "suggestions": [
    "Consider using UNION approach for multi-fact aggregations",
    "Ensure foreign keys are properly indexed"
  ],
  "security_analysis": {
    "sql_injection_risk": "low",
    "dangerous_patterns": []
  },
  "obqc_validation": {
    "all_identifiers_qualified": true,
    "unqualified_identifiers": []
  }
}
```

**Usage Example:**
```python
validate_sql_syntax(sql_query="SELECT * FROM public.customers WHERE id = 1")
```

---

### 15. `execute_sql_query`

**Description:** Execute SQL query with validation, fan-trap protection, and automatic schema context injection.

**Input Parameters:**
- `sql_query` (str, required): SQL SELECT statement (fully qualified identifiers required)
- `limit` (int, optional): Maximum rows to return (default: 1000, max: 10,000)
- `checklist_completed` (bool, optional): Pre-execution checklist confirmation
- `query_intent` (str, optional): **NEW in Phase 2!** Natural language description of query purpose.
  - Example: "Find total sales by customer" or "Show top 10 products by revenue"
  - If provided, used for accurate GraphRAG context retrieval
  - If not provided, intent will be extracted from SQL (less accurate)
  - **RECOMMENDED:** Always provide for best results

**Returns:**
```json
{
  "success": true,
  "data": [
    {"id": 1, "name": "John", "total": 1500.00},
    {"id": 2, "name": "Jane", "total": 2300.00}
  ],
  "columns": ["id", "name", "total"],
  "row_count": 2,
  "execution_time_ms": 45
}
```

**Phase 2 Enhancement:**
If GraphRAG is initialized, this tool automatically retrieves relevant schema context to enhance validation. Provide `query_intent` for most accurate context retrieval.

**Usage Examples:**
```python
# RECOMMENDED: With explicit query intent (Phase 2)
execute_sql_query(
  sql_query="SELECT id, name, SUM(amount) as total FROM public.orders GROUP BY id, name",
  limit=100,
  query_intent="Show total order amounts by customer"
)

# Basic: Without query intent (will auto-extract from SQL)
execute_sql_query(
  sql_query="SELECT * FROM public.customers WHERE id = 1",
  limit=10
)
```

---

## Visualization Tools

### 16. `generate_chart`

**Description:** Generate interactive or static charts from query results using Plotly.

**Input Parameters:**
- `data_source` (List[Dict] or str, required): Query result data
- `chart_type` (str, required): 'bar', 'line', 'scatter', or 'heatmap'
- `x_column` (str, required): Column name for X-axis
- `y_column` (str or List[str], optional): Column name(s) for Y-axis
- `color_column` (str, optional): Column for grouping/coloring
- `title` (str, optional): Chart title
- `chart_style` (str, optional): 'default', 'stacked', or 'grouped' (default: 'grouped')
- `width` (int, optional): Chart width (default: 800)
- `height` (int, optional): Chart height (default: 600)
- `sort_by` (str, optional): Sort column
- `sort_order` (str, optional): Sort order ('asc' or 'desc')
- `output_format` (str, optional): 'image' or 'interactive' (default: 'image')

**Returns:** Interactive chart via MCP-UI or PNG image

**Usage Example:**
```python
# Bar chart
result = execute_sql_query("SELECT region, SUM(sales) as total FROM sales GROUP BY region")
generate_chart(
  data_source=result['data'],
  chart_type='bar',
  x_column='region',
  y_column='total',
  title='Sales by Region'
)

# Multi-line chart
generate_chart(
  data_source=data,
  chart_type='line',
  x_column='month',
  y_column=['revenue', 'expenses', 'profit'],
  title='Monthly Financial Trends'
)

# Stacked bar chart
generate_chart(
  data_source=data,
  chart_type='bar',
  x_column='region',
  y_column='total',
  color_column='product_type',
  chart_style='stacked'
)
```

---

## GraphRAG Tools

### 17. `initialize_graphrag`

**Description:** Initialize GraphRAG for intelligent schema navigation and semantic search.

**Input Parameters:**
- `schema_name` (str, optional): Schema to initialize
- `embedding_model` (str, optional): "tfidf" or "sentence-transformers" (default: "tfidf")

**Returns:**
```
GraphRAG initialized successfully for schema 'public'!

📊 Statistics:
- Tables indexed: 25
- Columns indexed: 312
- Relationships: 18
- Vector embeddings: 337
- Communities detected: 5
- Embedding model: TF-IDF

✅ Ready for semantic search and intelligent context retrieval
```

**Usage Example:**
```python
# Initialize with default TF-IDF embeddings
initialize_graphrag(schema_name="public")

# Use sentence transformers (slower but more accurate)
initialize_graphrag(schema_name="public", embedding_model="sentence-transformers")
```

---

### 18. `graphrag_search`

**Description:** Search schema using natural language via GraphRAG semantic search.

**Input Parameters:**
- `query` (str, required): Natural language search query
- `top_k` (int, optional): Number of results to return (default: 5)
- `element_type` (str, optional): Filter by "table", "column", "relationship", or None

**Returns:**
```json
{
  "success": true,
  "query": "find customer and order information",
  "result_count": 5,
  "results": [
    {
      "type": "table",
      "name": "customers",
      "similarity": 0.89,
      "description": "Customer master data",
      "details": {...}
    },
    {
      "type": "table",
      "name": "orders",
      "similarity": 0.85,
      "description": "Order transactions"
    }
  ]
}
```

**Usage Example:**
```python
# Search for tables
graphrag_search(query="customer information", element_type="table")

# Search for columns
graphrag_search(query="email address", element_type="column", top_k=10)

# Search all elements
graphrag_search(query="sales and revenue data")
```

---

### 19. `graphrag_query_context`

**Description:** Get optimized context for SQL query generation using GraphRAG. **Main RAG retrieval function with 85-95% token savings.**

**Input Parameters:**
- `query` (str, required): Natural language description of query intent
- `max_tables` (int, optional): Maximum tables to include (default: 5)
- `max_columns` (int, optional): Maximum columns to include (default: 20)

**Returns:**
```json
{
  "success": true,
  "query": "show total sales by customer",
  "context": {
    "relevant_tables": [
      {
        "name": "customers",
        "similarity": 0.89,
        "columns": ["id", "name", "email"],
        "row_count": 15234
      },
      {
        "name": "orders",
        "similarity": 0.85,
        "columns": ["id", "customer_id", "amount", "order_date"]
      }
    ],
    "relevant_columns": [...],
    "relationships": [
      {
        "from": "orders.customer_id",
        "to": "customers.id",
        "type": "foreign_key"
      }
    ],
    "warnings": []
  },
  "usage_guidance": "..."
}
```

**Token Savings:** Returns 1k-5k tokens vs 36k-145k tokens (85-95% reduction)

**Usage Example:**
```python
graphrag_query_context(
  query="show total sales by customer in 2024",
  max_tables=3,
  max_columns=15
)
```

---

### 20. `graphrag_find_join_path`

**Description:** Find join path between two tables using GraphRAG graph traversal.

**Input Parameters:**
- `from_table` (str, required): Source table name
- `to_table` (str, required): Target table name
- `max_hops` (int, optional): Maximum joins allowed (default: 3)

**Returns:**
```json
{
  "success": true,
  "from": "orders",
  "to": "products",
  "hops": 2,
  "path": ["orders", "order_items", "products"],
  "joins": [
    {
      "from_table": "orders",
      "from_column": "id",
      "to_table": "order_items",
      "to_column": "order_id",
      "join_sql": "orders.id = order_items.order_id"
    },
    {
      "from_table": "order_items",
      "from_column": "product_id",
      "to_table": "products",
      "to_column": "id",
      "join_sql": "order_items.product_id = products.id"
    }
  ]
}
```

**Usage Example:**
```python
graphrag_find_join_path(from_table="orders", to_table="products", max_hops=3)
```

---

### 21. `graphrag_overview`

**Description:** Get GraphRAG schema overview with statistics and domain communities.

**Input Parameters:** None

**Returns:**
```json
{
  "success": true,
  "overview": {
    "schema_name": "public",
    "vector_store_stats": {
      "total_embeddings": 337,
      "tables": 25,
      "columns": 312,
      "model": "TF-IDF"
    },
    "graph_summary": {
      "nodes": 25,
      "edges": 18,
      "avg_degree": 1.44,
      "central_tables": ["orders", "customers"],
      "hub_tables": ["order_items"],
      "reference_tables": ["countries", "statuses"]
    },
    "communities": [
      {
        "id": 0,
        "size": 8,
        "tables": ["customers", "orders", "payments", ...],
        "suggested_name": "Customer Orders Domain"
      }
    ]
  }
}
```

**Usage Example:**
```python
graphrag_overview()
```

---

## RDF Store & SPARQL Tools

### 22. `store_ontology_in_rdf`

**Description:** Store current session ontology in persistent Oxigraph RDF store with SPARQL access.

**Input Parameters:**
- `schema_name` (str, optional): Schema name
- `graph_uri` (str, optional): Named graph URI

**Returns:**
```
Ontology stored successfully in RDF store!

📊 Statistics:
- Graph URI: http://example.com/ontology/public
- Triples stored: 1,247
- Storage location: tmp/oxigraph_store/

✅ Ready for SPARQL queries
```

**Usage Example:**
```python
store_ontology_in_rdf(schema_name="public")
```

---

### 23. `query_sparql`

**Description:** Execute SPARQL SELECT query against stored ontologies.

**Input Parameters:**
- `sparql_query` (str, required): SPARQL SELECT query string
- `timeout_seconds` (int, optional): Query timeout (default: 30)

**Returns:**
```json
{
  "success": true,
  "result_count": 25,
  "results": [
    {
      "table": "http://example.com/ontology/customers",
      "label": "Customer Master"
    }
  ],
  "query": "SELECT ?table ?label WHERE { ... }"
}
```

**Available Prefixes:**
- `rdf:` - http://www.w3.org/1999/02/22-rdf-syntax-ns#
- `rdfs:` - http://www.w3.org/2000/01/rdf-schema#
- `owl:` - http://www.w3.org/2002/07/owl#
- `xsd:` - http://www.w3.org/2001/XMLSchema#
- `db:` - Your ontology base URI

**Usage Example:**
```python
query_sparql(sparql_query='''
  PREFIX db: <http://example.com/ontology/>
  SELECT ?table ?label WHERE {
    ?table a db:Table .
    ?table rdfs:label ?label .
  }
  ORDER BY ?label
''')
```

---

### 24. `query_sparql_ask`

**Description:** Execute SPARQL ASK query (returns true/false).

**Input Parameters:**
- `sparql_query` (str, required): SPARQL ASK query

**Returns:**
```json
{
  "success": true,
  "result": true,
  "query": "ASK { ?table a db:Table }"
}
```

**Usage Example:**
```python
query_sparql_ask(sparql_query='''
  PREFIX db: <http://example.com/ontology/>
  ASK {
    ?table a db:Table .
    ?table db:tableName "customers" .
  }
''')
```

---

### 25. `add_rdf_knowledge`

**Description:** Add custom knowledge/metadata to the RDF store for documentation and learning.

**Input Parameters:**
- `subject` (str, required): Subject URI
- `predicate` (str, required): Predicate URI
- `object` (str, required): Object value (literal or URI)
- `metadata` (Dict, optional): Additional metadata as key-value pairs

**Returns:**
```
Knowledge added successfully!

📝 Triple added:
- Subject: http://example.com/ontology/customers
- Predicate: db:businessRule
- Object: "Customer emails must be unique"

✅ Knowledge now queryable via SPARQL
```

**Usage Example:**
```python
add_rdf_knowledge(
  subject="http://example.com/ontology/customers",
  predicate="db:businessRule",
  object="Customer emails must be unique across all records"
)
```

---

### 26. `list_tables_sparql`

**Description:** List all tables from stored ontology using SPARQL.

**Input Parameters:**
- `schema_graph` (str, optional): Graph URI to query (auto-detected if not specified)

**Returns:**
```json
{
  "success": true,
  "table_count": 25,
  "tables": [
    {
      "uri": "http://example.com/ontology/customers",
      "name": "customers",
      "label": "Customer Master"
    }
  ],
  "graph": "http://example.com/ontology/public"
}
```

**Usage Example:**
```python
list_tables_sparql()
```

---

### 27. `find_columns_by_type_sparql`

**Description:** Find columns by data type using SPARQL queries.

**Input Parameters:**
- `data_type` (str, required): SQL data type (e.g., "INTEGER", "VARCHAR", "DATE")
- `schema_graph` (str, optional): Graph URI

**Returns:**
```json
{
  "success": true,
  "data_type": "INTEGER",
  "column_count": 45,
  "columns": [
    {
      "uri": "http://example.com/ontology/customers_id",
      "table": "customers",
      "column": "id",
      "label": "Customer ID"
    }
  ]
}
```

**Usage Example:**
```python
find_columns_by_type_sparql(data_type="INTEGER")
find_columns_by_type_sparql(data_type="VARCHAR")
find_columns_by_type_sparql(data_type="DATE")
```

---

### 28. `get_rdf_store_stats`

**Description:** Get comprehensive statistics about the persistent RDF store.

**Input Parameters:** None

**Returns:**
```json
{
  "success": true,
  "stats": {
    "total_triples": 1247,
    "named_graphs": 1,
    "graphs": [
      {
        "uri": "http://example.com/ontology/public",
        "triple_count": 1247
      }
    ],
    "loaded_ontologies": ["public"],
    "store_location": "tmp/oxigraph_store/"
  }
}
```

**Usage Example:**
```python
get_rdf_store_stats()
```

---

## Common Workflows

### Workflow 1: Basic Schema Analysis

```python
# 1. Connect to database
connect_database(db_type="postgresql")

# 2. List available schemas
list_schemas()

# 3. Lightweight schema analysis (saves ~90% tokens)
analyze_schema(schema_name="public", lightweight=True)

# 4. Get details for specific tables on-demand
get_table_details(table_name="customers")
get_table_details(table_name="orders")

# 5. Sample data for exploration
sample_table_data(table_name="customers", limit=10)
```

**Token Usage:** ~5-10k tokens total

---

### Workflow 2: Ontology Generation & Enrichment

```python
# 1. Connect to database
connect_database(db_type="postgresql")

# 2. Generate ontology (auto-analyzes schema, auto-persists to RDF)
generate_ontology(schema_name="public")

# 3. Extract names for semantic improvement
suggest_semantic_names(ontology_file="ontology_public.ttl")

# 4. Apply LLM-suggested business-friendly names
suggestions = '''
{
  "classes": [
    {"original_name": "cust", "suggested_name": "Customer", "description": "..."}
  ],
  "properties": [...]
}
'''
apply_semantic_names(suggestions=suggestions, ontology_file="ontology_public.ttl")

# 5. Download improved ontology
download_ontology(schema_name="public", source="file")
```

**Token Usage:** ~2-5k tokens (with auto_persist=True)

---

### Workflow 3: GraphRAG-Powered Query Generation

```python
# 1. Connect to database
connect_database(db_type="postgresql")

# 2. Lightweight schema analysis
analyze_schema(schema_name="public", lightweight=True)

# 3. Initialize GraphRAG
initialize_graphrag(schema_name="public")

# 4. Get intelligent context for query
context = graphrag_query_context(
  query="show total sales by customer in 2024",
  max_tables=3,
  max_columns=15
)

# 5. Validate SQL before execution
validate_sql_syntax(sql_query="SELECT c.name, SUM(o.amount) ...")

# 6. Execute query
result = execute_sql_query(sql_query="...", limit=100)

# 7. Visualize results
generate_chart(
  data_source=result['data'],
  chart_type='bar',
  x_column='name',
  y_column='total_sales'
)
```

**Token Usage:** ~1-5k tokens (85-95% reduction vs full schema)

---

### Workflow 4: SPARQL Ontology Querying

```python
# 1. Connect and generate ontology
connect_database(db_type="postgresql")
generate_ontology(schema_name="public", auto_persist=True)

# 2. List all tables via SPARQL
list_tables_sparql()

# 3. Find specific column types
find_columns_by_type_sparql(data_type="INTEGER")

# 4. Custom SPARQL query
query_sparql(sparql_query='''
  PREFIX db: <http://example.com/ontology/>
  SELECT ?table ?column ?type WHERE {
    ?table a db:Table .
    ?table db:hasColumn ?col .
    ?col db:columnName ?column .
    ?col db:dataType ?type .
    FILTER(?type = "VARCHAR")
  }
''')

# 5. Add custom knowledge
add_rdf_knowledge(
  subject="http://example.com/ontology/customers",
  predicate="db:businessRule",
  object="Emails must be unique"
)

# 6. Get store statistics
get_rdf_store_stats()
```

**Token Usage:** ~1-3k tokens per query

---

### Workflow 5: Multi-Database Analysis

```python
# PostgreSQL
connect_database(db_type="postgresql")
analyze_schema(schema_name="public", lightweight=True)
generate_ontology(schema_name="public")

# Snowflake
connect_database(db_type="snowflake")
analyze_schema(schema_name="TPCDS", lightweight=True)
generate_ontology(schema_name="TPCDS")

# Compare schemas via SPARQL
query_sparql(sparql_query='''
  SELECT ?graph (COUNT(?table) as ?table_count) WHERE {
    GRAPH ?graph {
      ?table a db:Table .
    }
  }
  GROUP BY ?graph
''')
```

---

## Token Optimization Summary

| Approach | Token Usage | Reduction |
|----------|-------------|-----------|
| Full schema dump | 36k-145k | Baseline |
| Lightweight schema | 5-10k | ~90% |
| GraphRAG context | 1-5k | 85-95% |
| Ontology auto-persist | 1-2k | 94-98% |

**Best Practices:**
1. **Always use lightweight mode first**: `analyze_schema(lightweight=True)`
2. **Use GraphRAG for query generation**: `graphrag_query_context()`
3. **Auto-persist ontologies**: `generate_ontology(auto_persist=True)`
4. **Get table details on-demand**: `get_table_details()` only when needed
5. **Cache results**: Use session cache, reset with `reset_cache()` only when schema changes

---

## Error Handling

All tools return structured error responses:

```json
{
  "success": false,
  "error": "Connection failed: timeout",
  "error_type": "connection_error",
  "details": "Connection to host:port timed out after 30s"
}
```

Common error types:
- `connection_error` - Database connection issues
- `parameter_error` - Invalid or missing parameters
- `validation_error` - SQL validation failures
- `execution_error` - Query execution failures
- `internal_error` - Server-side errors

---

## Support

- **GitHub:** https://github.com/ralfbecher/orionbelt-analytics
- **Issues:** https://github.com/ralfbecher/orionbelt-analytics/issues
- **Documentation:** https://github.com/ralfbecher/orionbelt-analytics#readme

---

**Copyright 2025 RALFORION d.o.o. | Licensed under Apache License 2.0**
