# Analytical Session Workflow

**Purpose:** Complete guide to the optimal tool chain for database analysis sessions with OrionBelt Analytics.

---

## Overview

OrionBelt Analytics provides an intelligent workflow that maximizes efficiency through:
- **Auto-initialization** of GraphRAG and ontology generation
- **Token optimization** via auto-persist and ChromaDB
- **Semantic intelligence** through RDF ontologies and SPARQL

This skill explains the recommended tool chain for typical analytical sessions.

---

## Standard Workflow

### Phase 1: Connection & Discovery

#### 1.1 Connect to Database

**Tool:** `connect_database()`

**Purpose:** Establish secure connection to PostgreSQL, Snowflake, or ClickHouse

**Example:**
```
User: "Connect to my PostgreSQL database at localhost"

Claude calls:
connect_database(
  database_type="postgresql",
  host="localhost",
  database="mydb",
  username="user",
  password="***" // encrypted
)
```

**What happens:**
- ✅ Connection validated
- ✅ Credentials encrypted with master password
- ✅ Connection fingerprint created (prevents data collisions)
- ✅ Session initialized

**Next:** List available schemas

---

#### 1.2 List Schemas (Optional)

**Tool:** `list_schemas()`

**Purpose:** Discover available schemas in the database

**Example:**
```
User: "What schemas are available?"

Claude calls:
list_schemas()
```

**Output:**
```json
{
  "schemas": ["public", "analytics", "staging"],
  "count": 3
}
```

**Next:** Analyze specific schema

---

### Phase 2: Schema Analysis (Auto-Init Magic!)

#### 2.1 Analyze Schema

**Tool:** `analyze_schema()`

**Purpose:** Get complete schema structure with automatic GraphRAG initialization

**Example:**
```
User: "Analyze the public schema"

Claude calls:
analyze_schema(
  schema_name="public",
  lightweight=false
)
```

**What happens automatically:**
1. ✅ **Schema metadata retrieved** (tables, columns, relationships)
2. ✅ **GraphRAG auto-initializes** (background, 2-5 seconds)
   - Vector embeddings created
   - Graph structure built
   - ChromaDB storage (10-25x faster than JSON)
3. ✅ **Connection-scoped storage** (prevents collisions)
4. ✅ **Schema hash calculated** (for version detection)

**Server logs you'll see:**
```
INFO - 🤖 GraphRAG auto-init triggered for schema: public
INFO - ✅ GraphRAG auto-initialized successfully (2.34s)
INFO - 📊 Indexed 15 tables with their metadata
```

**Output:**
```json
{
  "schema_name": "public",
  "tables": [
    {
      "name": "customers",
      "columns": [...],
      "foreign_keys": [...]
    }
  ],
  "table_count": 15,
  "total_columns": 87
}
```

**Next:** Use intelligent table discovery or generate ontology

---

### Phase 3: Intelligent Discovery (GraphRAG-Powered)

#### 3.1 Find Related Tables

**No explicit tool call needed** - Claude uses GraphRAG internally

**Purpose:** Discover table relationships using graph algorithms

**Example:**
```
User: "What tables are related to customers?"

Claude internally:
- Queries GraphRAG graph structure
- Finds direct relationships (FKs)
- Finds indirect relationships (via joins)
- Returns semantic explanation
```

**Output:**
```
The "customers" table is related to:

Direct relationships:
1. orders - via customers.id → orders.customer_id
2. addresses - via customers.id → addresses.customer_id

Indirect relationships:
3. order_items - through orders (2 hops)
4. products - through orders → order_items (3 hops)
```

**Token savings:** 85-95% reduction vs loading full schema every time

---

#### 3.2 Find Join Paths

**No explicit tool call needed** - Claude uses GraphRAG internally

**Purpose:** Optimal join path discovery between any two tables

**Example:**
```
User: "How do I join customers and products?"

Claude internally:
- Uses graph algorithms (Dijkstra's shortest path)
- Supports mixed-direction paths (A → B ← C)
- Finds optimal join chain
```

**Output:**
```sql
-- Join path: customers → orders → order_items → products (3 hops)

SELECT
  c.name as customer_name,
  p.name as product_name
FROM customers c
INNER JOIN orders o ON c.id = o.customer_id
INNER JOIN order_items oi ON o.id = oi.order_id
INNER JOIN products p ON oi.product_id = p.id
```

**Benefit:** No manual relationship discovery needed

---

### Phase 4: Semantic Intelligence (Ontology)

#### 4.1 Generate Ontology

**Tool:** `generate_ontology()`

**Purpose:** Create RDF ontology with automatic persistence

**Example:**
```
User: "Generate an ontology for the public schema"

Claude calls:
generate_ontology(
  schema_name="public",
  auto_persist=true  // default - saves 99% tokens!
)
```

**What happens:**
1. ✅ **Ontology generated** (RDF/OWL in Turtle format)
2. ✅ **Auto-persisted to Oxigraph RDF store** (default behavior)
3. ✅ **Summary returned** (not full TTL - massive token savings)

**Output (Summary):**
```
✅ Ontology generated and stored successfully!

Schema: public
Tables: 15
Ontology file: ontology_public_20260311_160000.ttl
Storage location: tmp/
Graph URI: <http://example.com/schema/public>
Triples stored: 1,234

💾 Ontology is now persistent in Oxigraph RDF database.
📊 Use query_sparql() to explore the schema graph.
📥 Use download_ontology(schema_name="public") to get the TTL file.

Token savings: ~23,456 tokens saved by auto-persisting to RDF store!
```

**Token savings:** 99% (23k-94k tokens saved vs returning full TTL)

**Next:** Query ontology with SPARQL or download full TTL if needed

---

#### 4.2 Download Ontology (Optional)

**Tool:** `download_ontology()`

**Purpose:** Retrieve full ontology in Turtle format when needed

**Example:**
```
User: "Download the ontology"

Claude calls:
download_ontology(
  schema_name="public",
  source="rdf"  // from RDF store (recommended)
)
```

**Output:**
```json
{
  "success": true,
  "content": "@prefix ns: <http://example.com/ontology/> ...",
  "file_path": "tmp/ontology_public_export.ttl",
  "triple_count": 1234,
  "source": "rdf"
}
```

**Use cases:**
- Backup ontologies
- Import into external RDF tools (Protégé, TopBraid)
- Version control (commit .ttl to git)
- Offline analysis

---

#### 4.3 SPARQL Queries (Advanced)

**Tool:** `query_sparql()` or `list_tables_sparql()`

**Purpose:** Semantic queries over the ontology

**Example:**
```
User: "List all tables using SPARQL"

Claude calls:
list_tables_sparql()
```

**Custom SPARQL:**
```
User: "Find all columns with INTEGER type"

Claude calls:
query_sparql(
  sparql_query="""
    PREFIX db: <http://example.com/db#>
    SELECT ?table ?column
    WHERE {
      ?table db:hasColumn ?column .
      ?column db:dataType "INTEGER"
    }
  """
)
```

**Benefits:**
- Semantic reasoning over schema
- Complex pattern matching
- Business logic queries

---

### Phase 5: Data Exploration

#### 5.1 Sample Data

**Tool:** `sample_table_data()`

**Purpose:** Preview table contents with security controls

**Example:**
```
User: "Show me sample data from customers table"

Claude calls:
sample_table_data(
  table_name="customers",
  limit=10
)
```

**Output:**
```json
{
  "table": "customers",
  "rows": [
    {"id": 1, "name": "John Doe", "email": "john@example.com"},
    ...
  ],
  "row_count": 10,
  "total_rows": 1000
}
```

**Security:**
- Max 1000 rows per sample
- Read-only access
- No mutations allowed

---

### Phase 6: Query Execution

#### 6.1 Validate SQL (Recommended)

**Tool:** `validate_sql_syntax()`

**Purpose:** Validate SQL before execution (prevents errors)

**Example:**
```
User: "Validate this query: SELECT * FROM customers WHERE id = 1"

Claude calls:
validate_sql_syntax(
  sql_query="SELECT * FROM customers WHERE id = 1"
)
```

**Output:**
```json
{
  "is_valid": true,
  "query_type": "SELECT",
  "tables_referenced": ["customers"],
  "is_read_only": true
}
```

**Benefits:**
- Syntax checking
- Security validation (no DROP, DELETE, etc.)
- Fan-trap detection

---

#### 6.2 Execute SQL Query

**Tool:** `execute_sql_query()`

**Purpose:** Safe SQL execution with automatic protections

**Example:**
```
User: "Get all customers who ordered in 2024"

Claude calls:
execute_sql_query(
  sql_query="""
    SELECT c.name, COUNT(o.id) as order_count
    FROM customers c
    JOIN orders o ON c.id = o.customer_id
    WHERE o.order_date >= '2024-01-01'
    GROUP BY c.name
  """
)
```

**What happens:**
1. ✅ **Syntax validation**
2. ✅ **Fan-trap detection** (prevents data multiplication)
3. ✅ **Injection prevention** (parameterized queries)
4. ✅ **Timeout protection** (60s default)
5. ✅ **Result size limit** (max 10,000 rows)

**Output:**
```json
{
  "success": true,
  "rows": [
    {"name": "John Doe", "order_count": 5},
    ...
  ],
  "row_count": 42,
  "execution_time": 0.15
}
```

**Security features:**
- Read-only enforcement
- No DDL/DML commands
- Automatic cleanup

---

### Phase 7: Visualization

#### 7.1 Generate Chart

**Tool:** `generate_chart()`

**Purpose:** Interactive visualizations from query results

**Example:**
```
User: "Show me a bar chart of orders by month"

Claude calls:
generate_chart(
  data=[...],  // from previous query
  chart_type="bar",
  x_column="month",
  y_column="order_count",
  title="Orders by Month"
)
```

**Supported chart types:**
- `bar` - Bar charts
- `line` - Line graphs
- `scatter` - Scatter plots
- `pie` - Pie charts
- `area` - Area charts
- `histogram` - Histograms
- `box` - Box plots

**Output:**
- Base64-encoded image
- Embedded in MCP UI viewer
- Interactive (Plotly-based)

**Tip:** See `skill://chart-examples` for complete examples

---

## Optimization Tips

### Auto-Features (Enabled by Default)

**1. GraphRAG Auto-Init**
- Automatically triggers on `analyze_schema()`
- No manual initialization needed
- Configure: `AUTO_GRAPHRAG=true` in `.env`

**2. Auto-Persist Ontology**
- Automatically stores ontology in RDF store
- Returns summary instead of full TTL
- Saves 23k-94k tokens per generation

**3. ChromaDB Vector Storage**
- Automatically used if installed
- 10-25x faster than JSON
- 90% less memory usage

**4. Connection-Scoped Storage**
- Prevents data collisions
- Separate storage per database
- Schema change detection

---

## Common Workflows

### Workflow A: Quick Analysis

```
1. connect_database()
2. analyze_schema()
3. Ask: "What tables are related to X?"
4. sample_table_data()
5. execute_sql_query()
```

**Time:** 30-60 seconds
**Best for:** Quick data exploration

---

### Workflow B: Comprehensive Analysis

```
1. connect_database()
2. list_schemas()
3. analyze_schema()
4. generate_ontology()  // auto-persist enabled
5. Ask: "Find join path between X and Y"
6. validate_sql_syntax()
7. execute_sql_query()
8. generate_chart()
```

**Time:** 2-5 minutes
**Best for:** In-depth analysis with semantic layer

---

### Workflow C: Ontology-First Approach

```
1. connect_database()
2. analyze_schema()
3. generate_ontology()
4. download_ontology()  // for external tools
5. query_sparql()  // semantic queries
6. execute_sql_query()
```

**Time:** 3-7 minutes
**Best for:** Semantic analysis, data cataloging

---

## Error Handling

### Connection Issues

**Problem:** "Connection refused"

**Solutions:**
1. Check database host/port
2. Verify credentials
3. Check firewall rules
4. Use `diagnose_connection_issue()` tool

---

### Schema Not Found

**Problem:** "Schema 'xyz' does not exist"

**Solutions:**
1. Use `list_schemas()` to see available schemas
2. Check schema name spelling (case-sensitive)
3. Verify user has access permissions

---

### Query Timeout

**Problem:** "Query execution timeout"

**Solutions:**
1. Optimize query (add indexes)
2. Reduce result set (add WHERE clause)
3. Increase timeout in `.env`: `QUERY_TIMEOUT=120`

---

## Performance Expectations

### Schema Analysis
- **Small** (5-10 tables): 0.5-1.5s
- **Medium** (20-50 tables): 2-5s
- **Large** (100+ tables): 5-15s

### GraphRAG Auto-Init
- **Small**: +0.5s
- **Medium**: +2s
- **Large**: +5s

### Ontology Generation
- **Small** (500 triples): 1-3s
- **Medium** (2000 triples): 5-10s
- **Large** (5000 triples): 15-30s

### Query Execution
- **Simple SELECT**: 0.01-0.1s
- **Complex JOIN**: 0.1-5s
- **Aggregation**: 0.5-10s

---

## Advanced Features

### Fan-Trap Prevention

**Automatic detection** of fan-trap patterns in queries

**See:** `skill://fan-trap-prevention` for complete guide

**Example:**
```sql
-- ❌ Fan-trap (multiplicative JOIN)
SELECT c.name, COUNT(*) as count
FROM customers c
JOIN orders o ON c.id = o.customer_id
JOIN addresses a ON c.id = a.customer_id  -- TRAP!

-- ✅ Fixed with subquery
SELECT c.name, o.order_count, a.address_count
FROM customers c
JOIN (SELECT customer_id, COUNT(*) as order_count FROM orders GROUP BY customer_id) o
  ON c.id = o.customer_id
JOIN (SELECT customer_id, COUNT(*) as address_count FROM addresses GROUP BY customer_id) a
  ON c.id = a.customer_id
```

---

### SQL Best Practices

**Always qualify identifiers** in multi-table queries

**See:** `skill://sql-best-practices` for complete guide

**Example:**
```sql
-- ❌ Ambiguous
SELECT id, name FROM customers JOIN orders

-- ✅ Qualified
SELECT c.id, c.name FROM customers c JOIN orders o ON c.id = o.customer_id
```

---

## Tool Reference

### Core Tools (Most Used)

| Tool | Purpose | Phase |
|------|---------|-------|
| `connect_database()` | Establish connection | 1 |
| `analyze_schema()` | Get schema + auto-init GraphRAG | 2 |
| `generate_ontology()` | Create RDF ontology (auto-persist) | 4 |
| `execute_sql_query()` | Safe SQL execution | 6 |
| `generate_chart()` | Data visualization | 7 |

### Discovery Tools

| Tool | Purpose |
|------|---------|
| `list_schemas()` | List available schemas |
| `sample_table_data()` | Preview table data |

### Ontology Tools

| Tool | Purpose |
|------|---------|
| `download_ontology()` | Get full TTL file |
| `query_sparql()` | Semantic queries |
| `list_tables_sparql()` | List tables via SPARQL |

### Utility Tools

| Tool | Purpose |
|------|---------|
| `validate_sql_syntax()` | Validate before execution |
| `diagnose_connection_issue()` | Troubleshoot connections |
| `get_server_info()` | Server status and config |

---

## Best Practices

### 1. Always Connect First
Never skip connection - all tools require active database connection.

### 2. Use Auto-Init Features
Let GraphRAG and ontology auto-initialize - don't call them manually.

### 3. Validate Before Execute
Use `validate_sql_syntax()` before `execute_sql_query()` to catch errors early.

### 4. Leverage GraphRAG
Ask about table relationships - GraphRAG is much faster than manual discovery.

### 5. Auto-Persist by Default
Keep `auto_persist=true` (default) - only disable if you need full TTL immediately.

### 6. Sample Before Query
Use `sample_table_data()` to understand data before writing complex queries.

### 7. Check Server Logs
Watch server logs to see auto-init happening and verify features are active.

---

## Summary

**Minimal Workflow (3 steps):**
```
1. connect_database()
2. analyze_schema()  // auto-inits GraphRAG
3. execute_sql_query()
```

**Optimal Workflow (5 steps):**
```
1. connect_database()
2. analyze_schema()  // auto-inits GraphRAG
3. generate_ontology()  // auto-persist
4. execute_sql_query()
5. generate_chart()
```

**Advanced Workflow (7+ steps):**
```
1. connect_database()
2. list_schemas()
3. analyze_schema()
4. generate_ontology()
5. query_sparql()  // semantic discovery
6. validate_sql_syntax()
7. execute_sql_query()
8. generate_chart()
```

**Key Points:**
- ✅ GraphRAG auto-initializes (no manual setup)
- ✅ Ontology auto-persists (99% token savings)
- ✅ ChromaDB auto-used (10-25x faster)
- ✅ Connection-scoped (no collisions)
- ✅ Security built-in (injection prevention, read-only)

**Result:** Efficient, intelligent, secure database analysis with minimal token usage and maximum semantic power! 🚀
