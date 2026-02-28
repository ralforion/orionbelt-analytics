# Session & Knowledge Context Management Proposal

**Date:** 2026-02-27
**Status:** Draft for Discussion
**Author:** Analysis based on OrionBelt Analytics architecture

---

## Problem Statement

### Current Issues

1. **Manual Infrastructure Management**
   - LLMs must explicitly call `initialize_graphrag()` after schema analysis
   - No guarantee the LLM will make the right tool calls in the right order
   - Different LLM models (Sonnet, Haiku, etc.) behave inconsistently
   - Users must understand internal architecture to use effectively

2. **Fragmented State Management**
   - Schema cache in `session.schema_cache`
   - Ontology in `session.ontology_cache`
   - GraphRAG manager in `session.graphrag_manager`
   - Oxigraph store in `session.oxigraph_store`
   - No unified lifecycle management

3. **No Automatic Knowledge Building**
   - Vector embeddings not auto-created when schema is analyzed
   - RDF store not auto-populated when ontology is generated
   - No automatic invalidation when database connection changes
   - No background optimization or indexing

4. **Poor Session Boundaries**
   - Switching databases doesn't clear old state
   - No connection-scoped sessions
   - Cache invalidation is manual via `reset_cache()`
   - No automatic cleanup on disconnect

### Why This Matters

**Reliability:** Infrastructure operations must be deterministic and automatic, not dependent on LLM behavior.

**Performance:** Background initialization prevents blocking the LLM while building indexes.

**User Experience:** Users should think "connect → query" not "connect → analyze → initialize GraphRAG → generate ontology → store in RDF → query".

**Token Efficiency:** Automatic management eliminates the need for verbose instructions telling the LLM how to orchestrate tools.

---

## Proposed Solution: Automatic Context Management

### Core Principle

**"The MCP server should manage its own infrastructure lifecycle automatically based on user-initiated high-level operations."**

User triggers semantic operations (connect, query), server handles infrastructure (indexes, caches, stores) transparently.

---

## Design: Context Layers

### Layer 1: Connection Context (Automatic)

**Trigger:** `connect_database()` succeeds

**Automatic Actions:**
1. Create new session context keyed by connection fingerprint
2. Clear any previous session data for different connection
3. Initialize empty caches (schema, ontology, graphrag)
4. Set connection metadata (db_type, schema_name, timestamp)

**State:**
```python
class ConnectionContext:
    connection_id: str          # Hash of connection params
    db_type: str                # postgresql, snowflake, etc.
    connected_at: datetime
    default_schema: str
    session_state: SessionState # All caches and indexes
```

**Cache Key:** `{db_type}://{host}:{port}/{database}@{schema}`

**Behavior:**
- Switching databases auto-clears old context
- Reconnecting to same DB restores cached state
- Disconnect clears active context but preserves disk cache

---

### Layer 2: Schema Context (Automatic + Progressive)

**Trigger:** First schema-dependent operation (`analyze_schema`, `list_schemas`, `sample_table_data`)

**Automatic Actions (Background):**

**Phase 1: Lightweight Analysis (0-2 seconds)**
```
1. Analyze schema metadata (tables, relationships)
2. Store in session.schema_cache
3. Return to LLM immediately
```

**Phase 2: GraphRAG Initialization (Background, 2-10 seconds)**
```
4. Auto-initialize GraphRAG with TF-IDF embeddings
5. Build vector index for tables, columns, relationships
6. Detect communities
7. Store state to disk
8. Log: "GraphRAG initialized in background (5.2s)"
```

**Phase 3: Ontology Generation (Background, Optional)**
```
9. If RDF features needed, auto-generate ontology
10. Store in Oxigraph with auto_persist=True
11. Log: "RDF ontology stored (1,247 triples)"
```

**Progressive Disclosure:**
- Lightweight analysis returns immediately (LLM not blocked)
- Background tasks log completion
- Subsequent queries automatically use GraphRAG/RDF if available
- If LLM tries to use GraphRAG before ready, tool responds: "GraphRAG initializing in background (75% complete), falling back to full schema"

**State Tracking:**
```python
class SchemaContext:
    schema_name: str
    analyzed_at: datetime
    table_count: int

    # Analysis state
    lightweight_complete: bool = False
    graphrag_complete: bool = False
    ontology_complete: bool = False

    # Cached data
    schema_data: Dict
    graphrag_state_path: Path
    ontology_graph_uri: str
```

---

### Layer 3: Query Context (Automatic RAG)

**Trigger:** `execute_sql_query()` or `validate_sql_syntax()`

**Automatic Actions:**

**If GraphRAG available:**
```
1. Parse query intent from SQL
2. Auto-retrieve relevant context via graphrag_query_context()
3. Inject into validation/execution context
4. Return enhanced results
```

**If GraphRAG unavailable:**
```
1. Fall back to cached schema metadata
2. Use basic FK-based validation
3. Log: "GraphRAG not available, using basic validation"
```

**Smart Caching:**
- Cache query patterns and retrieved contexts
- Learn frequently accessed tables
- Pre-warm cache for common joins

---

### Layer 4: Knowledge Context (Learned Patterns)

**Trigger:** Repeated queries, user feedback, execution patterns

**Automatic Learning:**

**Pattern 1: Frequently Used Tables**
```
Track: Which tables appear in queries most often
Action: Pre-load their detailed metadata
Effect: Faster query context retrieval
```

**Pattern 2: Common Joins**
```
Track: Which join paths are used repeatedly
Action: Cache join patterns in RDF as "learned relationships"
Effect: Better join path suggestions
```

**Pattern 3: Business Rules**
```
Track: Validation warnings that users override
Action: Add as annotations to ontology
Effect: Reduced false positives
```

**Pattern 4: Semantic Mappings**
```
Track: User corrections to semantic names
Action: Store in RDF with provenance
Effect: Improved name suggestions over time
```

**Storage:**
- Vector DB: Weighted embeddings (boost frequent patterns)
- RDF Store: Custom triples with `db:learnedPattern` predicate
- Session state: In-memory cache for current session

---

## Implementation Strategy

### Phase 1: Automatic GraphRAG on Schema Analysis ✅

**Changes Required:**

1. **Modify `analyze_schema()` tool:**
```python
async def analyze_schema(schema_name: str, lightweight: bool = True):
    # 1. Analyze schema (current behavior)
    schema_data = await db_manager.analyze_schema(schema_name)
    session.schema_cache[schema_name] = schema_data

    # 2. AUTO-INITIALIZE GraphRAG in background
    if lightweight:  # Only for lightweight mode to avoid blocking
        asyncio.create_task(
            _auto_initialize_graphrag(schema_name, schema_data)
        )

    # 3. Return immediately (don't block LLM)
    return format_lightweight_response(schema_data)

async def _auto_initialize_graphrag(schema_name: str, schema_data: dict):
    """Background task: Initialize GraphRAG automatically."""
    try:
        logger.info(f"🔄 Auto-initializing GraphRAG for {schema_name}...")
        start = time.time()

        # Initialize GraphRAG
        if session.graphrag_manager is None:
            session.graphrag_manager = GraphRAGManager(
                base_uri=config.ontology_base_uri,
                embedding_model="tfidf"
            )

        session.graphrag_manager.initialize_from_schema(
            schema_name, schema_data
        )

        elapsed = time.time() - start
        logger.info(f"✅ GraphRAG initialized automatically ({elapsed:.1f}s)")
        session.graphrag_initialized = True

    except Exception as e:
        logger.warning(f"⚠️ GraphRAG auto-init failed: {e}")
        # Don't fail the main operation
```

**Benefits:**
- LLM doesn't need to call `initialize_graphrag()`
- No additional tool call = saved tokens
- Background initialization doesn't block LLM
- Graceful degradation if initialization fails

---

### Phase 2: Connection-Scoped Sessions

**Changes Required:**

1. **Add connection fingerprinting:**
```python
def _get_connection_id(db_manager: DatabaseManager) -> str:
    """Generate unique ID for current connection."""
    conn_info = db_manager.connection_info
    return hashlib.sha256(
        f"{conn_info['database_type']}://"
        f"{conn_info.get('host', '')}:{conn_info.get('port', '')}/"
        f"{conn_info.get('database', '')}"
        .encode()
    ).hexdigest()[:16]
```

2. **Modify `connect_database()` to auto-clear:**
```python
async def connect_database(db_type: str, ...):
    # Current connection logic
    success = db_manager.connect_xxx(...)

    if success:
        # NEW: Check if connection changed
        new_conn_id = _get_connection_id(db_manager)

        if session.connection_id != new_conn_id:
            logger.info(f"🔄 Connection changed, clearing old session")
            _clear_session_state(session)
            session.connection_id = new_conn_id
            session.connected_at = datetime.now()
```

3. **Add session state class:**
```python
@dataclass
class SessionState:
    connection_id: Optional[str] = None
    connected_at: Optional[datetime] = None

    # Caches
    schema_cache: Dict[str, Any] = field(default_factory=dict)
    ontology_cache: Dict[str, str] = field(default_factory=dict)

    # Indexes
    graphrag_manager: Optional[GraphRAGManager] = None
    graphrag_initialized: bool = False

    oxigraph_store: Optional[OxigraphStoreManager] = None
    oxigraph_initialized: bool = False

    # Metadata
    analyzed_schemas: Set[str] = field(default_factory=set)
    query_history: List[Dict] = field(default_factory=list)
```

---

### Phase 3: Smart Query Context Auto-Injection

**Changes Required:**

1. **Modify `execute_sql_query()` to auto-use GraphRAG:**
```python
async def execute_sql_query(sql_query: str, ...):
    # NEW: Auto-retrieve context if GraphRAG available
    context = None
    if session.graphrag_initialized:
        try:
            # Parse query intent (simple keyword extraction)
            query_intent = _extract_query_intent(sql_query)

            # Auto-retrieve relevant context
            context = session.graphrag_manager.get_query_context(
                query=query_intent,
                max_tables=3,
                max_columns=15
            )
            logger.info(f"📊 Auto-retrieved context: "
                       f"{len(context['relevant_tables'])} tables")
        except Exception as e:
            logger.warning(f"Context retrieval failed: {e}")

    # Continue with validation using context
    validation = validate_with_context(sql_query, context)
    ...
```

2. **Add query intent parser:**
```python
def _extract_query_intent(sql: str) -> str:
    """Extract natural language intent from SQL query."""
    # Simple heuristic: extract table names and column names
    tables = re.findall(r'FROM\s+(\w+)', sql, re.IGNORECASE)
    columns = re.findall(r'SELECT\s+(.*?)\s+FROM', sql, re.IGNORECASE)

    # Generate intent
    if tables and columns:
        return f"query {', '.join(tables)} for {columns[0]}"
    return "database query"
```

---

### Phase 4: Knowledge Learning & Persistence

**Changes Required:**

1. **Track query patterns:**
```python
class QueryPatternTracker:
    def __init__(self, store: OxigraphStoreManager):
        self.store = store
        self.session_patterns = defaultdict(int)

    def record_query(self, sql: str, tables: List[str]):
        """Record query pattern for learning."""
        # Track table usage frequency
        for table in tables:
            self.session_patterns[f"table:{table}"] += 1

        # Track join patterns
        if len(tables) > 1:
            join_key = "→".join(sorted(tables))
            self.session_patterns[f"join:{join_key}"] += 1

    def persist_patterns(self):
        """Save learned patterns to RDF store."""
        for pattern, count in self.session_patterns.items():
            if count >= 3:  # Threshold for "frequent"
                self._add_pattern_to_rdf(pattern, count)
```

2. **Add pattern-based optimizations:**
```python
class SmartContextRetriever:
    def __init__(self, graphrag: GraphRAGManager, patterns: QueryPatternTracker):
        self.graphrag = graphrag
        self.patterns = patterns

    def get_context(self, query: str) -> Dict:
        """Get context with pattern-based boosting."""
        # Get base context
        context = self.graphrag.get_query_context(query)

        # Boost frequently used tables
        for table in context['relevant_tables']:
            freq = self.patterns.get_frequency(f"table:{table['name']}")
            if freq > 5:
                table['boosted'] = True
                table['similarity'] *= 1.2  # Boost score

        return context
```

---

## Decision Points for Discussion

### 1. Background vs Blocking Initialization

**Option A: Background (Proposed)**
- ✅ LLM not blocked, faster response
- ✅ Progressive enhancement
- ❌ GraphRAG might not be ready for immediate use
- ❌ More complex state management

**Option B: Blocking**
- ✅ GraphRAG always ready
- ✅ Simpler state management
- ❌ LLM blocked during initialization (5-10s)
- ❌ Slower perceived performance

**Recommendation:** Background with fallback. If LLM queries before ready, fall back to basic schema.

---

### 2. Automatic Ontology Generation

**Option A: Always Auto-Generate**
- ✅ RDF always available
- ✅ SPARQL tools always work
- ❌ Slower initialization (extra 2-5s)
- ❌ Uses more disk space

**Option B: On-Demand Only**
- ✅ Faster initialization
- ✅ Only generate if SPARQL tools used
- ❌ First SPARQL query slow
- ❌ LLM might not know to trigger it

**Option C: Lazy + Background (Proposed)**
- ✅ Fast initialization
- ✅ Auto-generate in background after GraphRAG
- ✅ Ready for most SPARQL uses
- ❌ Slightly more complex

**Recommendation:** Lazy + Background. Start after GraphRAG completes.

---

### 3. Knowledge Persistence Scope

**Option A: Session-Only**
- ✅ Simple, no persistence complexity
- ❌ Learning lost between sessions
- ❌ No long-term optimization

**Option B: Global Persistence**
- ✅ Learning accumulates over time
- ✅ Server gets smarter with use
- ❌ Privacy concerns (cross-session data)
- ❌ Requires cleanup/invalidation strategy

**Option C: Per-Connection Persistence (Proposed)**
- ✅ Learning persists per database
- ✅ No cross-user privacy issues
- ✅ Automatic cleanup when DB connection removed
- ❌ Moderate complexity

**Recommendation:** Per-Connection Persistence. Store learned patterns keyed by connection fingerprint.

---

### 4. Explicit vs Implicit Control

**Option A: Fully Automatic (Proposed for most)**
- GraphRAG: Auto-initialize on schema analysis
- Ontology: Auto-generate in background
- Context: Auto-inject in queries
- Learning: Auto-persist patterns

**Option B: Explicit Opt-In**
- Add parameters: `auto_graphrag=True`, `auto_ontology=True`
- Let users control automation level
- More flexible but requires understanding

**Option C: Hybrid**
- Auto for core features (GraphRAG, context injection)
- Explicit for advanced features (learning, persistence)

**Recommendation:** Fully Automatic for Phase 1, add opt-out controls later if needed.

---

## Migration Path

### Step 1: Non-Breaking Changes (Recommended First)

1. Add background GraphRAG initialization to `analyze_schema()`
2. Add connection fingerprinting to `connect_database()`
3. Add query pattern tracking (passive, no behavior change)
4. Log all automatic actions clearly

**Testing:**
- Verify backward compatibility
- Test with different LLMs (Sonnet, Haiku)
- Measure performance impact
- Monitor logs for issues

### Step 2: Enhanced Automation

1. Auto-generate ontology in background
2. Auto-inject context in `execute_sql_query()`
3. Add smart caching for frequent patterns

### Step 3: Advanced Learning

1. Persist learned patterns to RDF
2. Implement pattern-based boosting
3. Add cross-session optimization

---

## Benefits Summary

### For Users
- **Simpler:** Just connect and query, infrastructure handled automatically
- **Faster:** Background initialization doesn't block interactions
- **Smarter:** Server learns patterns and optimizes over time

### For LLMs
- **Fewer Tools:** No need to orchestrate `initialize_graphrag`, `store_ontology_in_rdf`
- **Fewer Tokens:** No verbose instructions about tool sequencing
- **More Reliable:** No dependency on LLM remembering to initialize infrastructure

### For Developers
- **Cleaner:** Infrastructure concerns separated from user-facing operations
- **Testable:** Background tasks can be tested independently
- **Maintainable:** Clear lifecycle boundaries

---

## Risks & Mitigations

### Risk 1: Background Tasks Fail Silently

**Mitigation:**
- Log all background operations clearly
- Expose status via `get_server_info()`: `"graphrag_status": "initializing (75%)"`
- Graceful degradation if background tasks fail

### Risk 2: Increased Complexity

**Mitigation:**
- Start with Phase 1 only (auto GraphRAG)
- Add complexity incrementally
- Maintain explicit tools as fallback for advanced users

### Risk 3: Resource Usage

**Mitigation:**
- Make background initialization configurable via env var: `AUTO_GRAPHRAG=true`
- Add resource limits (max vector store size, max cached patterns)
- Implement cache eviction policies

### Risk 4: Breaking Changes

**Mitigation:**
- Keep all existing tools unchanged
- Add automation as enhancement, not replacement
- Version the session state format
- Provide migration guide if needed

---

## Implementation Priority

### P0 (Critical - Do First)
1. ✅ Auto-initialize GraphRAG on `analyze_schema(lightweight=True)`
2. ✅ Connection-scoped session clearing on `connect_database()`
3. ✅ Background task logging

### P1 (High Value)
4. Auto-generate ontology after GraphRAG completes
5. Auto-inject GraphRAG context in `execute_sql_query()`
6. Session state persistence to disk

### P2 (Nice to Have)
7. Query pattern tracking
8. Smart context boosting based on patterns
9. Learned pattern persistence to RDF

### P3 (Future)
10. Cross-session learning (with privacy controls)
11. Predictive pre-warming
12. Advanced optimization strategies

---

## Open Questions

1. **Initialization Timing:** Should we block schema analysis for GraphRAG or do it in background?
   - **Recommendation:** Background with progress logging

2. **Ontology Auto-Generation:** Always generate or only when SPARQL tools used?
   - **Recommendation:** Generate in background after GraphRAG

3. **User Visibility:** Should users see background task progress?
   - **Recommendation:** Yes, via logs and `get_server_info()` status field

4. **Opt-Out Controls:** Should there be env vars to disable automation?
   - **Recommendation:** Yes, add `AUTO_GRAPHRAG=true`, `AUTO_ONTOLOGY=true`

5. **Cross-Session Learning:** Store learned patterns per-connection or globally?
   - **Recommendation:** Per-connection for privacy, global as opt-in later

6. **Cache Invalidation:** When should we invalidate GraphRAG/ontology caches?
   - **Recommendation:** On schema changes (detected via timestamp), explicit `reset_cache()`, connection change

---

## Conclusion

**Core Recommendation:** Implement Phase 1 (Auto GraphRAG on Schema Analysis) immediately. This provides 80% of the value with minimal risk.

**Key Principle:** The MCP server should be smart about infrastructure management so the LLM can focus on user intent, not tool orchestration.

**Next Steps:**
1. Review and approve this proposal
2. Implement Phase 1 (auto GraphRAG)
3. Test with real workloads
4. Gather feedback
5. Proceed to Phase 2 if successful

---

## Appendix: Code Sketch for Phase 1

```python
# In src/main.py

async def analyze_schema(
    schema_name: Optional[str] = None,
    lightweight: bool = True,
    auto_init_graphrag: bool = True,  # NEW: explicit control
    ctx: Context = None
) -> str:
    """
    Analyze database schema structure.

    NEW: Automatically initializes GraphRAG in background (if auto_init_graphrag=True).
    """
    session = _get_session_state(ctx)

    # ... existing schema analysis code ...

    # NEW: Auto-initialize GraphRAG in background
    if auto_init_graphrag and lightweight:
        # Don't block, start background task
        asyncio.create_task(
            _auto_initialize_graphrag_background(
                schema_name=schema_name or default_schema,
                schema_data=schema_data,
                session=session,
                ctx=ctx
            )
        )
        logger.info(f"🔄 GraphRAG initialization started in background")

    # Return immediately (don't wait for GraphRAG)
    return format_lightweight_response(schema_data)


async def _auto_initialize_graphrag_background(
    schema_name: str,
    schema_data: Dict,
    session: SessionState,
    ctx: Context
):
    """
    Background task: Initialize GraphRAG automatically.

    This runs async after analyze_schema returns, so it doesn't block the LLM.
    """
    try:
        start_time = time.time()
        logger.info(f"🧠 Initializing GraphRAG for {schema_name}...")

        # Initialize GraphRAG manager if needed
        if session.graphrag_manager is None:
            config = config_manager.get_server_config()
            session.graphrag_manager = GraphRAGManager(
                base_uri=config.ontology_base_uri,
                embedding_model="tfidf"  # Fast default
            )

        # Build vector index
        session.graphrag_manager.initialize_from_schema(
            schema_name=schema_name,
            schema_data=schema_data
        )

        # Save state to disk
        output_dir = get_output_dir()
        session.graphrag_manager.save_state(output_dir)

        elapsed = time.time() - start_time
        session.graphrag_initialized = True
        session.analyzed_schemas.add(schema_name)

        logger.info(f"✅ GraphRAG initialized successfully ({elapsed:.2f}s)")
        logger.info(f"📊 Indexed {session.graphrag_manager.vector_store.vector_count} vectors")

        # OPTIONAL: Chain to ontology generation
        if os.getenv("AUTO_ONTOLOGY", "false").lower() == "true":
            await _auto_generate_ontology_background(schema_name, session, ctx)

    except Exception as e:
        logger.error(f"❌ GraphRAG auto-initialization failed: {e}", exc_info=True)
        # Don't fail the main operation - graceful degradation
        session.graphrag_initialized = False


async def _auto_generate_ontology_background(
    schema_name: str,
    session: SessionState,
    ctx: Context
):
    """
    Optional: Auto-generate ontology after GraphRAG completes.

    Only runs if AUTO_ONTOLOGY=true in environment.
    """
    try:
        logger.info(f"🏗️ Auto-generating ontology for {schema_name}...")
        start_time = time.time()

        # Generate ontology (reuse existing logic)
        # ... ontology generation code ...

        # Auto-persist to RDF store
        if OXIGRAPH_AVAILABLE:
            store = get_oxigraph_store(ctx)
            # ... store ontology ...
            logger.info(f"✅ Ontology stored in RDF ({elapsed:.2f}s)")

    except Exception as e:
        logger.error(f"⚠️ Ontology auto-generation failed: {e}")
```

---

**END OF PROPOSAL**
