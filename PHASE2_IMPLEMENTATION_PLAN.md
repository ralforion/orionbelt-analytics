# Phase 2 Implementation Plan: Enhanced Automation

**Date:** 2026-02-27
**Status:** 📋 PLANNED → 🚧 IN PROGRESS
**Builds on:** Phase 1 (Automatic GraphRAG Initialization)

---

## Overview

Phase 2 adds **automatic ontology generation** and **smart query context injection** to further reduce manual tool orchestration and improve query generation accuracy.

---

## Goals

1. **Auto-generate ontology** in background after GraphRAG completes
2. **Auto-inject GraphRAG context** into SQL query validation/execution
3. **Persist session state** to disk for recovery
4. **Add progress visibility** in `get_server_info()`

---

## Features to Implement

### 1. Automatic Ontology Generation (Priority: High)

**Trigger:** After GraphRAG initialization completes successfully

**Implementation:**
- Chain `_auto_generate_ontology_background()` after GraphRAG finishes
- Use `auto_persist=True` by default (store in Oxigraph)
- Only generate if `AUTO_ONTOLOGY=true` in environment
- Log progress and statistics

**Expected Flow:**
```
analyze_schema(lightweight=True)
  → Background: Initialize GraphRAG (5-10s)
  → Background: Generate Ontology (3-7s)
  → Background: Store in Oxigraph (1-2s)
  → Total: 9-19s (still non-blocking!)
```

**Benefits:**
- SPARQL tools work immediately without explicit `generate_ontology()` call
- RDF store always populated after schema analysis
- Further token savings (23k-94k tokens by not returning full ontology)

---

### 2. Smart Query Context Injection (Priority: High)

**Trigger:** When `execute_sql_query()` or `validate_sql_syntax()` is called

**Implementation:**
- Detect if GraphRAG is initialized
- Extract query intent from SQL (simple heuristic)
- Auto-call `graphrag_query_context()` internally
- Inject relevant context into validation
- Enhance error messages with context

**Query Intent Extraction:**
```python
def _extract_query_intent(sql: str) -> str:
    """Extract natural language intent from SQL."""
    # Parse table names
    tables = re.findall(r'FROM\s+(\w+)', sql, re.IGNORECASE)

    # Parse aggregations
    aggs = re.findall(r'(SUM|AVG|COUNT|MAX|MIN)\s*\(', sql, re.IGNORECASE)

    # Generate intent
    if tables and aggs:
        return f"aggregate {', '.join(aggs)} from {', '.join(tables)}"
    elif tables:
        return f"query {', '.join(tables)}"
    return "database query"
```

**Benefits:**
- Better validation using relevant schema context
- Improved fan-trap detection
- More accurate warnings
- LLM doesn't need to call `graphrag_query_context()` explicitly

---

### 3. Session State Persistence (Priority: Medium)

**Purpose:** Recover GraphRAG state across server restarts

**Implementation:**
- Save session metadata to JSON after major operations
- Include: connection_id, analyzed_schemas, graphrag_state_path
- Load on server start if connection matches
- Clear on connection change

**State File Format:**
```json
{
  "connection_id": "a7f3b2c194e8d7f6",
  "connected_at": "2026-02-27T12:00:00Z",
  "analyzed_schemas": ["public", "analytics"],
  "graphrag_state_files": {
    "public": "tmp/graphrag_state_public.pkl",
    "analytics": "tmp/graphrag_state_analytics.pkl"
  },
  "ontology_graphs": {
    "public": "http://example.com/ontology/public"
  }
}
```

**Benefits:**
- Faster server restarts (no re-initialization needed)
- Preserves work across sessions
- Reduces redundant analysis

---

### 4. Progress Visibility (Priority: Low)

**Enhancement:** Add status fields to `get_server_info()`

**New Fields:**
```json
{
  "server_name": "OrionBelt Analytics",
  "background_tasks": {
    "graphrag_initialization": {
      "status": "complete",
      "schema": "public",
      "completed_at": "2026-02-27T12:00:15Z",
      "duration_seconds": 5.2
    },
    "ontology_generation": {
      "status": "in_progress",
      "schema": "public",
      "started_at": "2026-02-27T12:00:15Z",
      "progress": 75
    }
  }
}
```

**Benefits:**
- LLM can check if background tasks completed
- Better debugging
- User visibility into server state

---

## Implementation Tasks

### Task 1: Add Ontology Auto-Generation Function
- [ ] Create `_auto_generate_ontology_background()` async function
- [ ] Chain after GraphRAG completes in `_auto_initialize_graphrag_background()`
- [ ] Add `AUTO_ONTOLOGY` environment variable (default: false for Phase 2)
- [ ] Log progress and handle errors gracefully
- [ ] Store in Oxigraph with auto_persist=True

### Task 2: Modify GraphRAG Background Init to Chain Ontology
- [ ] Check `AUTO_ONTOLOGY` setting after GraphRAG completes
- [ ] Call ontology generation if enabled
- [ ] Pass schema_name and session context
- [ ] Update session state after ontology completes

### Task 3: Add Query Context Auto-Injection
- [ ] Create `_extract_query_intent(sql)` helper function
- [ ] Modify `execute_sql_query()` to auto-retrieve context
- [ ] Modify `validate_sql_syntax()` to use context
- [ ] Enhance validation with GraphRAG insights
- [ ] Add fallback if GraphRAG not ready

### Task 4: Add Session State Persistence
- [ ] Create `_save_session_state(session)` function
- [ ] Create `_load_session_state(connection_id)` function
- [ ] Save after: GraphRAG init, ontology generation, connection change
- [ ] Load on server start if connection matches
- [ ] Handle corrupted/missing state files gracefully

### Task 5: Enhance get_server_info with Progress
- [ ] Add `background_tasks` tracking to SessionData
- [ ] Update `get_server_info()` to include task status
- [ ] Track start/completion times
- [ ] Add progress percentage for long tasks

### Task 6: Update Documentation
- [ ] Update `.env.template` with AUTO_ONTOLOGY
- [ ] Update MCP_TOOLS_REFERENCE.md
- [ ] Create PHASE2_IMPLEMENTATION_SUMMARY.md
- [ ] Update testing instructions

---

## Environment Variables

```env
# Phase 1 (existing)
AUTO_GRAPHRAG=true

# Phase 2 (new)
AUTO_ONTOLOGY=false  # Conservative default, enable after testing

# Phase 2 (optional)
SESSION_STATE_PERSIST=true  # Enable session persistence
SESSION_STATE_DIR=tmp/sessions/  # Where to store session files
```

---

## Code Structure

```
src/main.py additions:

# New async functions
async def _auto_generate_ontology_background(...)
async def _extract_query_intent(sql: str) -> str

# Modified functions
async def _auto_initialize_graphrag_background(...)
  → Chain to _auto_generate_ontology_background if enabled

async def execute_sql_query(...)
  → Add context auto-injection

async def validate_sql_syntax(...)
  → Add context-aware validation

# New helper functions
def _save_session_state(session: SessionData) -> None
def _load_session_state(connection_id: str) -> Optional[Dict]

# Modified SessionData class
class SessionData:
    background_tasks: Dict[str, Dict] = {}  # Task tracking
```

---

## Testing Strategy

### Unit Tests
1. Test ontology auto-generation function
2. Test query intent extraction
3. Test session state save/load
4. Test chaining of background tasks

### Integration Tests
1. Full workflow: connect → analyze → wait → verify ontology exists
2. Test context injection improves validation
3. Test state persistence across "restarts"
4. Test progress tracking in get_server_info

### Manual Tests
1. Enable AUTO_ONTOLOGY=true, run workflow, check RDF store
2. Run SQL query without explicit context retrieval
3. Restart server, verify GraphRAG state restored
4. Check get_server_info shows background task status

---

## Risk Mitigation

### Risk 1: Ontology Generation Fails
**Impact:** RDF store not populated, SPARQL tools don't work
**Mitigation:**
- Graceful degradation, log error
- User can still call `generate_ontology()` explicitly
- Don't fail main operation

### Risk 2: Increased Background Processing Time
**Impact:** Longer wait for full initialization (9-19s vs 5-10s)
**Mitigation:**
- Keep AUTO_ONTOLOGY=false by default in Phase 2
- Let users opt-in after testing
- Still non-blocking, doesn't affect LLM

### Risk 3: Context Injection Overhead
**Impact:** Every query retrieves context (adds latency)
**Mitigation:**
- Cache query contexts by SQL pattern
- Only retrieve if GraphRAG available
- Fast TF-IDF search (<100ms typically)

### Risk 4: State File Corruption
**Impact:** Can't restore session state
**Mitigation:**
- JSON validation on load
- Fallback to fresh initialization
- Don't fail server startup

---

## Performance Targets

| Metric | Target | Notes |
|--------|--------|-------|
| Ontology generation time | 3-7s | Background, non-blocking |
| Context retrieval time | <100ms | Per SQL query |
| State save time | <50ms | After operations |
| State load time | <200ms | On server start |
| Total background time (GraphRAG + Ontology) | 9-19s | Still faster than manual! |

---

## Success Criteria

- [ ] Ontology auto-generates after GraphRAG completes
- [ ] RDF store populated automatically
- [ ] SQL validation uses GraphRAG context automatically
- [ ] Session state persists and restores correctly
- [ ] `get_server_info()` shows background task progress
- [ ] No breaking changes to Phase 1 functionality
- [ ] All tests pass
- [ ] Documentation updated

---

## Rollback Plan

If Phase 2 causes issues:

1. **Disable auto-ontology:**
   ```env
   AUTO_ONTOLOGY=false
   ```

2. **Disable auto-context:**
   - Comment out context injection in `execute_sql_query`
   - Users can still call `graphrag_query_context()` explicitly

3. **Disable state persistence:**
   ```env
   SESSION_STATE_PERSIST=false
   ```

4. **Full rollback:**
   - Keep Phase 1 features (auto GraphRAG)
   - Remove Phase 2 additions
   - No impact on Phase 1 functionality

---

## Timeline Estimate

| Task | Estimated Time | Priority |
|------|---------------|----------|
| Ontology auto-generation | 2-3 hours | High |
| Context auto-injection | 2-3 hours | High |
| Session state persistence | 1-2 hours | Medium |
| Progress tracking | 1 hour | Low |
| Testing & Documentation | 2 hours | High |
| **Total** | **8-11 hours** | |

---

## Phase 3 Preview

After Phase 2 stabilizes, Phase 3 will add:
- Query pattern learning (frequent tables, common joins)
- Smart context boosting based on usage
- Persistent learned patterns in RDF
- Predictive pre-warming

---

## Decision Points

### Q1: Should AUTO_ONTOLOGY default to true or false?
**Recommendation:** Start with `false` in Phase 2, enable `true` in Phase 3 after testing

**Reasoning:**
- Conservative approach reduces risk
- Users can opt-in to test
- Gives time to monitor performance

### Q2: Should context injection be always-on or opt-in?
**Recommendation:** Always-on if GraphRAG available, with caching

**Reasoning:**
- Improves validation quality automatically
- Fast enough (<100ms) not to cause issues
- Can add opt-out flag if needed later

### Q3: Should state persistence be enabled by default?
**Recommendation:** Yes, enabled by default

**Reasoning:**
- Low risk, high value
- Improves restart performance
- Easy to disable if issues arise

---

## Next Steps After Phase 2

1. **Monitor Performance:** Track background task times, context retrieval latency
2. **Gather Feedback:** See if users notice improvements
3. **Iterate:** Adjust defaults based on real-world usage
4. **Plan Phase 3:** Begin work on learning and optimization features

---

**Status:** 📋 Ready for Implementation
**Estimated Completion:** 1-2 days
**Complexity:** Medium (builds on Phase 1 foundation)
