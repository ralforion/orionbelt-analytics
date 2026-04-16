[<- Back to README](../README.md)

# GraphRAG Deep Dive

## What is GraphRAG

GraphRAG is a Graph-based Retrieval Augmented Generation subsystem built into OrionBelt Analytics. It provides schema intelligence by combining two complementary techniques:

- **Graph traversal** over foreign key relationships, supporting paths up to 12 hops deep, to discover how tables connect across complex schemas.
- **Vector embeddings** for semantic similarity, enabling natural language search over tables, columns, and relationships without requiring exact name matches.

Together, these techniques allow an LLM to navigate large database schemas efficiently -- finding relevant tables, discovering join paths, detecting fan-trap risks, and generating optimized query context -- all without scanning the entire schema on every request.

The subsystem lives under `src/graphrag/` and is composed of four core modules:

| Module | Responsibility |
|---|---|
| `manager.py` | Orchestrator that coordinates all GraphRAG operations |
| `embedder.py` | Generates vector embeddings for tables, columns, and relationships |
| `retriever.py` | Graph traversal, join path discovery, and fan-trap detection |
| `community_detector.py` | Clusters related tables into logical domains |
| `vector_store_chromadb.py` | Persistent vector storage using ChromaDB (HNSW indexing) |

## How It Works

### Schema Discovery Workflow

GraphRAG initialization follows a four-step pipeline, triggered automatically when `analyze_schema()` completes:

```
analyze_schema()
    |
    v
1. Create Embeddings
    |  - SchemaEmbedder.batch_embed_schema() processes all tables
    |  - Generates vectors for tables, columns, and relationships
    |  - Text representation includes table names, column names/types,
    |    comments, and FK context
    |  - Uses TF-IDF by default (sentence-transformers optional)
    |
    v
2. Build Vector Store
    |  - ChromaDB stores embeddings with metadata
    |  - HNSW algorithm provides automatic indexing
    |  - Persistent on disk at tmp/chromadb/{connection_id}/
    |  - Supports filtered search by element type (table/column/relationship)
    |
    v
3. Build Relationship Graph
    |  - NetworkX directed graph from FK relationships
    |  - Nodes = tables (with column count, comments)
    |  - Edges = foreign keys (with column mappings)
    |  - Supports bidirectional and mixed-direction path finding
    |
    v
4. Detect Communities
       - Label propagation algorithm on undirected graph view
       - Groups related tables into logical clusters
       - Identifies central tables and suggests domain names
       - Provides schema overview statistics
```

### What Gets Embedded

The embedder creates three types of vector representations:

**Table embeddings** encode the table name (split on underscores for word-level semantics), any table comment, all column names with their data types, and FK relationship targets. This means a search for "customer orders" can find an `order_header` table even if the word "customer" only appears in its FK to a `customer` table.

**Column embeddings** encode the parent table name, column name, data type, PK/FK status, and any column comment. Searching for "email address" finds `customers.email_addr` through semantic similarity.

**Relationship embeddings** encode the source table, target table, join column pairs, and relationship type. This allows searching for "how orders connect to products" to surface the specific FK paths.

### Key Operations

Once initialized, GraphRAG exposes these MCP tools:

- **`graphrag_search(query, top_k, element_type)`** -- Semantic search across the schema. Returns matching tables, columns, or relationships ranked by similarity score.

- **`graphrag_query_context(query, max_tables, max_columns)`** -- The primary RAG retrieval function. Returns a minimal, optimized context dictionary with relevant tables, columns, join paths, and fan-trap warnings. Includes a token estimate so the LLM can gauge context window usage.

- **`graphrag_find_join_path(from_table, to_table, max_hops)`** -- Finds the shortest join path between any two tables. Tries directed paths in both directions and an undirected view, then picks the shortest. Returns full join specifications with column mappings.

- **`graphrag_overview()`** -- Returns schema statistics: total tables and relationships, top central tables (by degree centrality), hub tables (many outgoing FKs), reference tables (many incoming FKs), community summaries, and suggested domain names.

### Fan-Trap Detection

GraphRAG includes automatic fan-trap detection during context retrieval. When a table has outgoing foreign keys to multiple other tables, it flags a potential data multiplication risk and recommends CTE or UNION ALL patterns. This integrates with OrionBelt's broader fan-trap prevention strategy that operates at the `execute_sql_query()` level.

## GraphRAG as Foundation for OBML

GraphRAG serves as the schema intelligence layer that bridges raw database structure and semantic business models. The workflow connecting OrionBelt Analytics GraphRAG to OrionBelt Semantic Layer OBML (OrionBelt Markup Language) model creation follows four stages:

### 1. Schema Discovery (Analytics + GraphRAG)

The starting point. Connect to a database, run `analyze_schema()`, and GraphRAG auto-initializes. At this stage you have:

- A complete graph of table relationships
- Vector embeddings for semantic search
- Community-detected domain clusters
- Fan-trap awareness across the schema

This is pure technical discovery -- no business semantics yet.

### 2. OBML Model Creation (LLM Queries GraphRAG for Relationships)

An LLM uses GraphRAG to construct an OBML semantic model. Rather than scanning hundreds of tables, the LLM:

- Uses `graphrag_search()` to find tables relevant to a business domain (e.g., "revenue analysis")
- Uses `graphrag_find_join_path()` to discover how those tables connect
- Uses `graphrag_query_context()` to get optimized, minimal schema context
- Uses community information to identify natural domain boundaries

GraphRAG provides the relationship intelligence that makes OBML model creation accurate and efficient. The LLM does not need to hold the entire schema in context -- it queries GraphRAG for exactly what it needs.

### 3. Business Queries (Semantic Layer)

Once the OBML model is created, the OrionBelt Semantic Layer handles business user queries. The semantic model provides pre-defined metrics, dimensions, and validated join paths. This is the steady-state for routine analytical queries.

### 4. Ad-hoc Technical Queries (Analytics + GraphRAG)

For exploratory or technical queries that fall outside the semantic model, users return to Analytics with GraphRAG. The graph and embeddings persist, so `graphrag_query_context()` can generate optimized SQL context for any ad-hoc question across the full schema -- not just the subset modeled in OBML.

### The Connection

GraphRAG is what makes OBML model creation scalable. Without it, an LLM would need the entire schema in its context window to understand relationships. With GraphRAG, it retrieves only the relevant subset, discovers join paths programmatically, and gets fan-trap warnings before they become data quality issues. The OBML model then captures this intelligence in a reusable semantic layer.

## Key Benefits

**Zero-setup intelligence.** GraphRAG initializes automatically as part of `analyze_schema()`. No separate configuration, no manual graph definition, no embedding model training. Connect to a database, analyze the schema, and GraphRAG is ready.

**Automatic relationship discovery.** Foreign key relationships are extracted from the database catalog and built into a traversable graph. Join paths between any two tables are found algorithmically (shortest path with up to 12 hops), including mixed-direction paths where FK arrows point in different directions.

**Semantic similarity.** Natural language queries find relevant schema elements even without exact name matches. The embedding model captures the semantic context of table names, column types, comments, and relationship targets. Searching for "customer purchases" finds `order_line_items` through its FK to `customers` and its column semantics.

**Foundation for OBML.** GraphRAG provides the relationship intelligence that LLMs need to create accurate OBML semantic models. Instead of dumping the entire schema into the context window, the LLM queries GraphRAG for targeted relationship discovery, reducing token usage by 85-95% while improving accuracy.

**Community detection.** Large schemas with hundreds of tables are automatically clustered into logical domains. The community detector identifies which tables belong together, finds central tables within each cluster, and suggests domain names based on naming patterns. This gives both humans and LLMs a navigable map of the schema.

**Fan-trap awareness.** GraphRAG detects potential fan-trap scenarios during context retrieval and flags them before queries are written. Tables that bridge multiple relationships are identified, and CTE/UNION ALL patterns are recommended to prevent data multiplication.

## Workspace Persistence

GraphRAG state persists across sessions. All three components -- vector store, relationship graph, and community assignments -- are saved to disk under the connection-specific directory:

```
tmp/
  chromadb/{connection_id}/       # ChromaDB persistent storage (automatic)
  {connection_id}/
    graph_combined.json           # Combined graph (all schemas, with schema_names list)
    vector_store_{schema}.json    # Vector store JSON export (backup, per-schema)
    graph_{schema}.json           # Per-schema graph backup (backward compat)
    communities_{schema}.json     # Community assignments and summaries
```

GraphRAG is connection-scoped and accumulative. Each `analyze_schema()` call adds tables to the same graph and vector store, enabling cross-schema join path discovery and unified semantic search. The combined state file (`graph_combined.json`) stores all accumulated schemas in a single graph; per-schema files are saved alongside for backward compatibility.

ChromaDB uses its own persistent storage that reconnects automatically when a new `GraphRAGManager` is created with the same `connection_id`. The JSON files serve as backup and contain the `tables_info` needed to rebuild the NetworkX graph and community assignments.

### Restoring a Workspace

When reconnecting to the same database in a new session, `connect_database()` automatically detects and restores the workspace:

```
connect_database("postgresql")
# → auto-restores schema cache, ontology, RDF store, and GraphRAG
```

This restores:

1. **Schema cache** -- The analyzed table metadata, so `analyze_schema()` does not need to re-query the database catalog.
2. **Ontology** -- The generated RDF/OWL file and its loaded state.
3. **RDF store** -- The Oxigraph triple store with SPARQL query support.
4. **GraphRAG** -- Vector embeddings (via ChromaDB reconnection), relationship graph (rebuilt from saved `tables_info`), and community assignments.

After auto-restore, all GraphRAG tools (`graphrag_search`, `graphrag_query_context`, `graphrag_find_join_path`, `graphrag_overview`) work immediately without re-analysis. The workspace metadata file tracks which components were initialized and when.

If multiple schemas exist in the workspace, all schemas are auto-restored on reconnect -- each schema's cache and ontology are loaded, and the connection-scoped GraphRAG state (covering all schemas) is restored once. Ontology state is per-schema (switching schemas preserves each schema's ontology), while GraphRAG is accumulative (all schemas share one graph for cross-schema discovery).
