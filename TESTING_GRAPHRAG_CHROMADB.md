# Testing GraphRAG and ChromaDB Features in Claude Desktop

**Date:** 2026-03-03
**Features:** GraphRAG integration, ChromaDB vector storage, Auto-persist optimization

## Overview

OrionBelt Analytics now includes:
- **GraphRAG**: Intelligent schema navigation with graph algorithms (85-95% token reduction)
- **ChromaDB**: High-performance vector storage (10-25x faster than JSON)
- **Auto-persist**: Automatic RDF storage (99% token savings on ontology generation)

These features work **automatically** in the background, but here's how to verify they're working!

---

## Prerequisites

1. **Server Running**: Start the OrionBelt Analytics MCP server
2. **Claude Desktop**: Connected to the MCP server
3. **Database**: PostgreSQL, Snowflake, or ClickHouse connection available

---

## Test 1: Verify GraphRAG Auto-Initialization

GraphRAG automatically initializes when you analyze a schema. Here's how to test:

### Step 1: Connect to Database

In Claude Desktop, ask:

```
Connect to my PostgreSQL database at localhost with database "mydb"
```

Claude will use the `connect_database()` tool with your credentials.

### Step 2: Analyze Schema

Ask Claude to analyze a schema:

```
Analyze the "public" schema and show me the table structure
```

Claude will call `analyze_schema(schema_name="public")`.

### Step 3: Check Server Logs

In your terminal where the server is running, you should see:

```
INFO - 🤖 GraphRAG auto-init triggered for schema: public
INFO - ✅ GraphRAG auto-initialized successfully (2.34s)
INFO - 📊 Indexed 15 tables with their metadata
```

**What this means:**
- ✅ GraphRAG initialized automatically in the background
- ✅ Table relationships indexed into graph structure
- ✅ Vector embeddings created for semantic search
- ✅ ChromaDB used if available, JSON fallback otherwise

---

## Test 2: Verify ChromaDB Vector Storage

ChromaDB is used automatically when available. Here's how to verify:

### Check Server Logs

When GraphRAG initializes, you should see:

```
INFO - Using ChromaDB for vector storage (10-25x faster than JSON)
```

If ChromaDB is not available:

```
WARNING - Using JSON-based vector store (ChromaDB not available)
```

### Verify Storage Location

After schema analysis, check the file system:

```bash
ls -la tmp/chromadb/
```

You should see a directory named after your connection ID:

```
tmp/chromadb/a7f3b2c1e8d9.../
```

Inside, you'll find ChromaDB's persistence files:

```
tmp/chromadb/a7f3b2c1e8d9.../
  ├── chroma.sqlite3
  └── ... (other ChromaDB files)
```

**What this means:**
- ✅ ChromaDB is installed and working
- ✅ Vector embeddings persisted to disk
- ✅ 10-25x faster search than JSON files
- ✅ 90% less memory usage

---

## Test 3: Test Intelligent Table Discovery

GraphRAG enables intelligent table suggestions. Here's how to test:

### Ask for Related Tables

After analyzing a schema, ask Claude:

```
What tables are related to the "customers" table?
```

Claude will use GraphRAG's `find_related_tables()` under the hood.

### Expected Response

Claude should return:

```
The "customers" table is related to:
1. orders - via foreign key (customers.id → orders.customer_id)
2. addresses - via foreign key (customers.id → addresses.customer_id)
3. payments - indirectly through orders table

These relationships were found using graph traversal algorithms.
```

**What this means:**
- ✅ GraphRAG found relationships via graph algorithms
- ✅ Direct and indirect relationships identified
- ✅ Foreign key traversal working correctly

---

## Test 4: Test Join Path Discovery

GraphRAG can find optimal join paths between tables.

### Ask About Join Path

```
How do I join the "customers" and "order_items" tables?
```

### Expected Response

Claude should provide:

```
To join customers and order_items:

JOIN path (2 hops):
1. customers → orders (customers.id = orders.customer_id)
2. orders → order_items (orders.id = order_items.order_id)

Full JOIN query:
SELECT *
FROM customers c
INNER JOIN orders o ON c.id = o.customer_id
INNER JOIN order_items oi ON o.id = oi.order_id
```

**What this means:**
- ✅ GraphRAG found shortest path (2 hops)
- ✅ Mixed-direction paths supported (A → B ← C)
- ✅ Optimal join conditions identified

---

## Test 5: Verify Auto-Persist (RDF Store)

When you generate an ontology, it's automatically persisted to Oxigraph RDF store.

### Generate Ontology

Ask Claude:

```
Generate an ontology for the "public" schema
```

Claude will call `generate_ontology(schema_name="public", auto_persist=True)`.

### Check Response

Claude should return a **summary** instead of full TTL:

```
✅ Ontology generated and stored successfully!

Schema: public
Tables: 15
Ontology file: ontology_public_20260303_123045.ttl
Storage location: tmp/
Graph URI: <http://example.com/schema/public>
Triples stored: 1,234

💾 Ontology is now persistent in Oxigraph RDF database.
📊 Use query_sparql() to explore the schema graph.
📥 Use download_ontology(schema_name="public") to get the TTL file.

Token savings: ~23,456 tokens saved by auto-persisting to RDF store!
```

**What this means:**
- ✅ Auto-persist enabled (default behavior)
- ✅ Ontology stored in RDF database
- ✅ 99% token savings (summary vs full TTL)
- ✅ Full TTL available via `download_ontology()`

### Verify RDF Storage

Check the Oxigraph store directory:

```bash
ls -la tmp/oxigraph/*/store/
```

You should see Oxigraph's RDF database files.

---

## Test 6: Download Full Ontology (Optional)

If you need the full TTL file, you can download it:

### Ask Claude

```
Download the ontology for the "public" schema
```

Claude will call `download_ontology(schema_name="public")`.

### Expected Response

Claude should provide:

```json
{
  "success": true,
  "file_path": "tmp/ontology_public_export.ttl",
  "file_size": 45678,
  "triple_count": 1234,
  "graph_uri": "http://example.com/schema/public",
  "source": "rdf"
}
```

And the full TTL content will be included.

**What this means:**
- ✅ Can retrieve full ontology when needed
- ✅ Exported from RDF store (not cached file)
- ✅ Triple count matches what was stored

---

## Test 7: SPARQL Queries (Advanced)

You can query the RDF store directly using SPARQL.

### Ask Claude

```
List all tables in the ontology using SPARQL
```

Claude will call `list_tables_sparql()` or construct a custom SPARQL query.

### Expected Response

```
Found 15 tables via SPARQL:
- customers
- orders
- order_items
- products
- ...
```

**What this means:**
- ✅ SPARQL endpoint working
- ✅ RDF data queryable
- ✅ Semantic queries enabled

---

## Configuration Options

### Enable/Disable Auto-Features

In `.env` file:

```bash
# Auto-initialize GraphRAG on schema analysis
AUTO_GRAPHRAG=true

# Auto-generate ontology after GraphRAG
AUTO_ONTOLOGY=false  # Conservative default

# ChromaDB storage (automatic if installed)
# No config needed - auto-detected
```

### Adjust Persistence

```bash
# Output directory for all generated files
OUTPUT_DIR=tmp

# For production, use persistent storage
OUTPUT_DIR=/var/lib/orionbelt
```

---

## Performance Expectations

### GraphRAG Initialization
- **Small schema** (5-10 tables): 0.5-1.5 seconds
- **Medium schema** (20-50 tables): 2-5 seconds
- **Large schema** (100+ tables): 5-15 seconds

### ChromaDB vs JSON
- **Search speed**: 10-25x faster (50ms → 2ms)
- **Memory usage**: 90% less (10MB → 1MB)
- **Disk usage**: 40% smaller (5MB → 3MB)

### Auto-Persist Token Savings
- **Small ontology** (500 triples): ~23k tokens saved
- **Medium ontology** (2000 triples): ~65k tokens saved
- **Large ontology** (5000 triples): ~94k tokens saved

---

## Troubleshooting

### GraphRAG Not Initializing

**Symptom:** No GraphRAG log messages after schema analysis.

**Check:**
1. Is `AUTO_GRAPHRAG=true` in `.env`?
2. Did schema analysis succeed?
3. Check server logs for errors

**Fix:** Set `AUTO_GRAPHRAG=true` and restart server.

---

### ChromaDB Not Being Used

**Symptom:** Server logs show "Using JSON-based vector store".

**Check:**
```bash
pip list | grep chromadb
```

**Fix:** Install ChromaDB:
```bash
pip install chromadb>=0.4.0
# or
uv pip install chromadb>=0.4.0
```

---

### Auto-Persist Not Working

**Symptom:** Ontology returns full TTL instead of summary.

**Check:**
1. Is Oxigraph installed? `pip list | grep pyoxigraph`
2. Check server logs for RDF store errors

**Fix:** Install Oxigraph:
```bash
pip install pyoxigraph>=0.3.22
```

Or explicitly disable auto-persist:
```python
generate_ontology(schema_name="public", auto_persist=False)
```

---

### RDF Store Not Found

**Symptom:** `download_ontology()` says "Graph is empty or not found".

**Check:**
1. Did you generate ontology first?
2. Check if RDF store directory exists: `ls tmp/oxigraph/`

**Fix:** Generate ontology first:
```
Generate an ontology for the public schema
```

---

## Quick Test Script

Run this test in Claude Desktop to verify everything works:

```
1. Connect to my PostgreSQL database
2. Analyze the "public" schema
3. Show me tables related to "customers"
4. Generate an ontology for this schema
5. Download the ontology
```

**Expected:**
- ✅ Connection succeeds
- ✅ Schema analyzed with GraphRAG auto-init
- ✅ Related tables found via graph algorithms
- ✅ Ontology generated with auto-persist (summary response)
- ✅ Full TTL available via download

---

## Visual Indicators in Server Logs

Look for these indicators that features are working:

### ✅ GraphRAG Active
```
INFO - 🤖 GraphRAG auto-init triggered for schema: public
INFO - ✅ GraphRAG auto-initialized successfully (2.34s)
```

### ✅ ChromaDB Active
```
INFO - Using ChromaDB for vector storage (10-25x faster than JSON)
```

### ✅ Auto-Persist Active
```
INFO - Auto-persisted ontology to Oxigraph: 1,234 triples in graph <...>
INFO - Token savings: ~23,456 tokens saved by auto-persisting to RDF store!
```

---

## Summary

**GraphRAG** and **ChromaDB** work **automatically** - you don't need to do anything special!

Just:
1. Connect to database
2. Analyze schema
3. Ask questions about tables/relationships
4. Generate ontologies

The features activate automatically in the background, providing:
- 🚀 10-25x faster vector search (ChromaDB)
- 🧠 85-95% token reduction (GraphRAG)
- 💾 99% token savings on ontology (auto-persist)

Enjoy the performance boost! 🎉

---

**Documentation:**
- `CHROMADB_UPGRADE.md` - ChromaDB implementation details
- `DATA_LIFECYCLE_MANAGEMENT.md` - GraphRAG architecture
- `PHASE3A_CONNECTION_SCOPED_RDF.md` - RDF store details
- `CODE_REVIEW_FIXES_2026-02-28.md` - Recent bug fixes
