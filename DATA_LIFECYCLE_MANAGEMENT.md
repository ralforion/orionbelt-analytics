# Data Lifecycle Management - Improvement Plan

**Date:** 2026-02-27
**Status:** 📋 PROPOSAL
**Context:** Current implementation lacks proper lifecycle management for Vector DB and RDF Store

---

## Current Problems

### **Vector DB (GraphRAG)**
- ❌ No versioning - latest overwrites previous
- ❌ No expiration/TTL policy
- ❌ No cleanup mechanism
- ❌ No metadata tracking (when created, which DB connection, schema hash)
- ❌ No tool to inspect or export data
- ❌ No multi-schema support in single session
- ❌ Orphaned files when connection changes

### **RDF Store (Ontology)**
- ❌ Multiple `analyze_schema()` calls create duplicate TTL files
- ❌ No automatic cleanup of old snapshots
- ❌ Named graph gets overwritten silently (no versioning)
- ❌ No schema change detection (no hash/checksum)
- ❌ No garbage collection for old graphs
- ❌ TTL files accumulate in `tmp/` folder forever

### **Session Management**
- ❌ Connection change clears session but not disk files
- ❌ No persistent session state (loses GraphRAG on restart)
- ❌ No recovery mechanism for interrupted initialization

---

## Proposed Solutions

### **1. Vector DB Lifecycle Enhancement**

#### **A. Add Metadata to GraphRAG State**

**Current:**
```python
# Just pickles the GraphRAGManager object
with open(state_file, 'wb') as f:
    pickle.dump(graphrag_manager, f)
```

**Proposed:**
```python
state = {
    "version": "1.0",
    "created_at": "2026-02-27T12:00:00Z",
    "schema_name": "public",
    "connection_fingerprint": "a7f3b2c194e8d7f6",
    "schema_hash": "sha256_of_schema_structure",  # Detect schema changes
    "db_connection": {
        "host": "localhost",
        "database": "mydb",
        "schema": "public"
    },
    "statistics": {
        "table_count": 25,
        "column_count": 312,
        "relationship_count": 18,
        "embedding_count": 337
    },
    "graphrag_manager": graphrag_manager,  # The actual object
    "expires_at": "2026-03-27T12:00:00Z"  # Optional TTL
}

with open(state_file, 'wb') as f:
    pickle.dump(state, f)
```

**Benefits:**
- Detect schema changes (rebuild if hash differs)
- Track which DB connection this belongs to
- Auto-expire old data
- Better debugging

---

#### **B. Add Version Management**

**File naming:**
```
tmp/graphrag_state_public_v1_20260227.pkl
tmp/graphrag_state_public_v2_20260228.pkl
```

**Metadata file:**
```json
// tmp/graphrag_metadata_public.json
{
  "current_version": 2,
  "versions": [
    {
      "version": 1,
      "file": "graphrag_state_public_v1_20260227.pkl",
      "created_at": "2026-02-27T12:00:00Z",
      "schema_hash": "abc123",
      "status": "archived"
    },
    {
      "version": 2,
      "file": "graphrag_state_public_v2_20260228.pkl",
      "created_at": "2026-02-28T10:00:00Z",
      "schema_hash": "def456",
      "status": "active"
    }
  ],
  "retention_policy": {
    "keep_latest": 3,
    "max_age_days": 30
  }
}
```

**New MCP Tools:**
```python
list_graphrag_versions(schema_name="public")
# Returns version history

rollback_graphrag_version(schema_name="public", version=1)
# Revert to previous version

cleanup_graphrag_versions(schema_name="public", keep_latest=3)
# Delete old versions
```

---

#### **C. Add Export/Import Tools**

**New MCP Tools:**

```python
export_graphrag_data(
    schema_name="public",
    format="json",  # or "csv", "graphml"
    output_path="exports/"
)
# Returns:
# - schema_graph.graphml (NetworkX graph for Gephi/Cytoscape)
# - embeddings.csv (vector embeddings)
# - communities.json (domain groupings)
# - metadata.json (statistics)
```

```python
import_graphrag_data(
    schema_name="public",
    import_path="exports/schema_graph.graphml"
)
# Rebuild GraphRAG from exported data
```

**Use Cases:**
- Backup/restore GraphRAG state
- Share schema analysis with team
- Visualize in external tools (Gephi, Neo4j)
- Migrate between environments

---

### **2. RDF Store Lifecycle Enhancement**

#### **A. Add Ontology Versioning**

**Current behavior:**
```
# Multiple analyze_schema() calls create:
tmp/ontology_public_20260227_120000.ttl
tmp/ontology_public_20260227_150000.ttl  # Duplicate!
tmp/ontology_public_20260228_100000.ttl  # Another duplicate!

# But same named graph in RDF store gets overwritten each time
```

**Proposed: Version-aware named graphs**

```python
# Store each version in separate graph
graph_uri_v1 = "http://example.com/ontology/public/v1"
graph_uri_v2 = "http://example.com/ontology/public/v2"

# Track active version
active_graph = "http://example.com/ontology/public"  # alias to latest
```

**Metadata tracking:**
```json
// tmp/ontology_metadata_public.json
{
  "schema_name": "public",
  "current_version": 2,
  "active_graph": "http://example.com/ontology/public/v2",
  "versions": [
    {
      "version": 1,
      "graph_uri": "http://example.com/ontology/public/v1",
      "ttl_file": "tmp/ontology_public_v1_20260227.ttl",
      "created_at": "2026-02-27T12:00:00Z",
      "schema_hash": "abc123",
      "triple_count": 1200,
      "status": "archived"
    },
    {
      "version": 2,
      "graph_uri": "http://example.com/ontology/public/v2",
      "ttl_file": "tmp/ontology_public_v2_20260228.ttl",
      "created_at": "2026-02-28T10:00:00Z",
      "schema_hash": "def456",
      "triple_count": 1247,
      "status": "active"
    }
  ]
}
```

**New MCP Tools:**

```python
list_ontology_versions(schema_name="public")
# Returns version history with diffs

compare_ontology_versions(schema_name="public", v1=1, v2=2)
# Returns:
# - Added tables: 2
# - Removed columns: 1
# - Modified relationships: 3

switch_ontology_version(schema_name="public", version=1)
# Makes v1 the active graph for SPARQL queries

cleanup_ontology_versions(schema_name="public", keep_latest=3)
# Deletes old graphs and TTL files
```

---

#### **B. Schema Change Detection**

**Add schema hashing:**

```python
def calculate_schema_hash(tables_info: List[TableInfo]) -> str:
    """Generate deterministic hash of schema structure."""
    schema_data = {
        "tables": sorted([
            {
                "name": t.name,
                "columns": sorted([c.name for c in t.columns]),
                "primary_keys": sorted(t.primary_keys),
                "foreign_keys": sorted([f.name for f in t.foreign_keys])
            }
            for t in tables_info
        ], key=lambda x: x["name"])
    }
    return hashlib.sha256(json.dumps(schema_data).encode()).hexdigest()
```

**Smart regeneration:**

```python
# Before regenerating ontology
current_hash = calculate_schema_hash(tables_info)
metadata = load_ontology_metadata(schema_name)

if metadata and metadata["current_version"]["schema_hash"] == current_hash:
    logger.info("⏭️ Schema unchanged, skipping ontology regeneration")
    return  # Reuse existing
else:
    logger.info("🔄 Schema changed, generating new ontology version")
    version = metadata["current_version"] + 1 if metadata else 1
    generate_new_version(version, current_hash)
```

---

#### **C. Automatic Cleanup Policies**

**Configuration in `.env`:**

```env
# Retention policies
GRAPHRAG_KEEP_VERSIONS=3
GRAPHRAG_MAX_AGE_DAYS=30

ONTOLOGY_KEEP_VERSIONS=5
ONTOLOGY_MAX_AGE_DAYS=60

# Auto-cleanup on startup
AUTO_CLEANUP_ON_STARTUP=true
```

**Cleanup scheduler:**

```python
async def _auto_cleanup_old_data():
    """Background cleanup based on retention policies."""

    # GraphRAG cleanup
    graphrag_versions = list_all_graphrag_versions()
    for schema_name, versions in graphrag_versions.items():
        keep = int(os.getenv("GRAPHRAG_KEEP_VERSIONS", 3))
        max_age = int(os.getenv("GRAPHRAG_MAX_AGE_DAYS", 30))

        # Delete versions beyond retention
        for v in versions[keep:]:
            if days_old(v) > max_age:
                delete_graphrag_version(schema_name, v)

    # Ontology cleanup (similar)
```

**New MCP Tool:**

```python
run_cleanup(
    cleanup_type="all",  # or "graphrag", "ontology", "tmp_files"
    dry_run=True  # Show what would be deleted
)
```

---

### **3. Session State Persistence**

**Goal:** Survive server restarts without re-initialization

**Implementation:**

```json
// tmp/session_state.json
{
  "connection_id": "a7f3b2c194e8d7f6",
  "connected_at": "2026-02-27T12:00:00Z",
  "db_connection": {
    "type": "postgresql",
    "host": "localhost",
    "port": 5432,
    "database": "mydb"
  },
  "analyzed_schemas": {
    "public": {
      "analyzed_at": "2026-02-27T12:05:00Z",
      "schema_hash": "abc123",
      "graphrag_version": 2,
      "graphrag_file": "tmp/graphrag_state_public_v2.pkl",
      "ontology_version": 2,
      "ontology_graph": "http://example.com/ontology/public/v2"
    }
  },
  "session_expires_at": "2026-02-28T12:00:00Z"
}
```

**On server startup:**

```python
async def restore_session_if_valid():
    """Restore session state on server restart."""

    if not os.path.exists("tmp/session_state.json"):
        return

    state = load_json("tmp/session_state.json")

    # Validate session hasn't expired
    if datetime.now() > parse_datetime(state["session_expires_at"]):
        logger.info("⏰ Session expired, starting fresh")
        return

    # Verify connection still matches
    current_fingerprint = _get_connection_fingerprint(...)
    if current_fingerprint != state["connection_id"]:
        logger.info("🔄 Connection changed, starting fresh")
        return

    # Restore GraphRAG managers
    for schema, info in state["analyzed_schemas"].items():
        graphrag_file = info["graphrag_file"]
        if os.path.exists(graphrag_file):
            logger.info(f"♻️ Restoring GraphRAG for schema '{schema}'")
            with open(graphrag_file, 'rb') as f:
                session.graphrag_manager = pickle.load(f)
                session.graphrag_initialized = True
```

---

### **4. Enhanced Monitoring & Introspection**

**New MCP Tool: `get_data_lifecycle_status()`**

```python
{
  "graphrag": {
    "public": {
      "current_version": 2,
      "file": "tmp/graphrag_state_public_v2.pkl",
      "created_at": "2026-02-28T10:00:00Z",
      "file_size": "2.3 MB",
      "schema_hash": "def456",
      "expires_at": "2026-03-28T10:00:00Z",
      "archived_versions": 1
    }
  },
  "ontology": {
    "public": {
      "current_version": 2,
      "active_graph": "http://example.com/ontology/public/v2",
      "ttl_file": "tmp/ontology_public_v2.ttl",
      "triple_count": 1247,
      "created_at": "2026-02-28T10:00:00Z",
      "file_size": "156 KB",
      "archived_versions": 1
    }
  },
  "rdf_store": {
    "total_triples": 2494,
    "total_graphs": 3,
    "store_size": "4.2 MB",
    "location": "tmp/oxigraph_store/"
  },
  "disk_usage": {
    "total_size": "12.5 MB",
    "graphrag_files": "4.8 MB",
    "ontology_files": "3.2 MB",
    "rdf_store": "4.2 MB",
    "tmp_files": "0.3 MB"
  },
  "cleanup_recommendations": [
    "2 archived GraphRAG versions can be deleted (save 4.5 MB)",
    "3 duplicate ontology TTL files found",
    "RDF store has 1 orphaned graph (no active reference)"
  ]
}
```

---

## Implementation Priority

### **Phase 3A: Critical Fixes** (High Priority)
1. ✅ Add schema hash detection (prevent unnecessary regeneration)
2. ✅ Add GraphRAG metadata wrapper (version, timestamps)
3. ✅ Implement basic cleanup tool
4. ✅ Add `get_data_lifecycle_status()` introspection tool

### **Phase 3B: Versioning** (Medium Priority)
5. ✅ Add ontology version tracking
6. ✅ Add GraphRAG version tracking
7. ✅ Implement version comparison tools

### **Phase 3C: Session Persistence** (Medium Priority)
8. ✅ Save session state to disk
9. ✅ Restore session on startup
10. ✅ Add session validation

### **Phase 3D: Advanced Features** (Low Priority)
11. ✅ Export/import tools
12. ✅ Automatic cleanup scheduler
13. ✅ Retention policy enforcement

---

## External Tool Integration

### **Enable Oxigraph HTTP Server**

**New MCP Tool:**

```python
start_oxigraph_server(
    port=7878,
    bind_address="0.0.0.0",
    read_only=True
)
# Starts Oxigraph in server mode
# Returns: "SPARQL endpoint available at http://localhost:7878/query"
```

**Benefits:**
- Use Apache Jena Fuseki UI to explore RDF
- Connect GraphDB, RDF4J Workbench
- Share SPARQL endpoint with team
- Test queries in browser

---

### **Export for External Tools**

**New formats:**

```python
export_graphrag_data(
    schema_name="public",
    format="neo4j"  # Creates Cypher import script
)

export_ontology(
    schema_name="public",
    format="rdf-xml"  # For Protégé
)
```

---

## Testing Plan

### **Test Scenarios:**

1. **Schema Change Detection**
   - Analyze schema → hash saved
   - Add column to DB
   - Re-analyze → new version created (hash changed)
   - Drop column → another version
   - Restore DB → reuses existing version (hash matches)

2. **Cleanup**
   - Create 10 versions
   - Run cleanup with `keep_latest=3`
   - Verify only 3 versions remain

3. **Session Persistence**
   - Connect DB, analyze schema
   - Shutdown server
   - Restart server
   - Verify GraphRAG works without re-init

4. **Version Rollback**
   - Create v1, v2, v3
   - Switch to v1
   - Run SPARQL query → returns v1 data
   - Switch to v3 → returns v3 data

---

## Success Criteria

- [ ] Schema hash prevents unnecessary regeneration
- [ ] Old versions automatically cleaned up
- [ ] GraphRAG state persists across restarts
- [ ] Ontology versions tracked and queryable
- [ ] External tools can access RDF data (Oxigraph server)
- [ ] Disk usage stays bounded (no infinite growth)
- [ ] Clear introspection of data lifecycle status

---

## Breaking Changes

**None!** All changes are additive and backward compatible.

- Existing workflows continue working
- Old state files can be migrated automatically
- New features are opt-in

---

## Next Steps

1. Review this proposal
2. Prioritize features (which are must-have vs nice-to-have?)
3. Implement Phase 3A (critical fixes)
4. Test with real database
5. Iterate based on feedback

---

**Date:** 2026-02-27
**Status:** 📋 AWAITING REVIEW
**Estimated Implementation:** 2-3 days for Phase 3A
