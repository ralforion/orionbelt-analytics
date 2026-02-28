# Phase 1 Testing Instructions

## Quick Manual Test (Recommended)

Since the test script requires a running server with database connection, here's a simple manual test you can do:

### Prerequisites
1. PostgreSQL database configured in `.env`
2. MCP server running (`uv run server.py`)
3. Claude Desktop or MCP client connected

### Test Steps

#### Test 1: Verify AUTO_GRAPHRAG is Enabled
```bash
# Check your .env file has:
grep AUTO_GRAPHRAG .env
# Should show: AUTO_GRAPHRAG=true
```

#### Test 2: Connect and Analyze Schema
Open Claude Desktop and run:

```
1. Connect to database:
   connect_database(db_type="postgresql")

   Expected log output:
   🔗 Initial connection established: <fingerprint>...

2. Analyze schema (lightweight mode):
   analyze_schema(schema_name="public", lightweight=True)

   Expected log output:
   🔄 GraphRAG auto-initialization started in background

3. Wait 5-10 seconds, then check logs:
   Expected:
   🧠 Auto-initializing GraphRAG for schema 'public'...
   ✅ GraphRAG auto-initialized successfully (X.Xs)
   📊 Indexed N vectors (M tables)
```

#### Test 3: Verify GraphRAG is Ready
```
4. Use GraphRAG search:
   graphrag_search("customer tables")

   Expected:
   - Should return results immediately
   - No error about GraphRAG not initialized
```

#### Test 4: Test Connection Change Detection
```
5. Connect to different database (or change .env and reconnect):
   connect_database(db_type="postgresql")

   Expected log output:
   🔄 Connection changed (old: <id1>..., new: <id2>...)
   🧹 Clearing session state (connection change)
   ✅ Session state cleared
```

#### Test 5: Test Opt-Out
```
6. Set AUTO_GRAPHRAG=false in .env
7. Restart server
8. Run analyze_schema again

   Expected:
   - NO log about GraphRAG initialization
   - GraphRAG not automatically initialized
```

---

## Automated Test (Requires Setup)

### Setup for Automated Testing

1. **Install dependencies:**
   ```bash
   uv sync
   ```

2. **Configure database in `.env`:**
   ```env
   POSTGRES_HOST=localhost
   POSTGRES_PORT=5432
   POSTGRES_DATABASE=testdb
   POSTGRES_USERNAME=testuser
   POSTGRES_PASSWORD=testpass
   ```

3. **Run test script:**
   ```bash
   uv run python test_phase1_auto_graphrag.py
   ```

### Expected Test Output

```
============================================================
Phase 1 Implementation Test Suite
Testing: Automatic GraphRAG Initialization
============================================================

Database Configuration:
  Host: localhost
  Port: 5432
  Database: testdb
  Username: testuser

============================================================
RUNNING SYNCHRONOUS TESTS
============================================================

============================================================
TEST 1: Connection Fingerprinting
============================================================
✅ PASS - Connection Fingerprinting: Generated valid fingerprint: a7f3b2c194e8d7f6

============================================================
TEST 2: Session State Clearing
============================================================
✅ PASS - Session State Clearing: All state cleared successfully

============================================================
TEST 3: Connection Change Detection
============================================================
✅ PASS - Connection Change Detection: Consistent fingerprints for same connection

============================================================
TEST 4: Environment Variable Control
============================================================
✅ PASS - Environment Variable Control: AUTO_GRAPHRAG=enabled

============================================================
RUNNING ASYNC TESTS
============================================================

============================================================
TEST 5: Background GraphRAG Initialization
============================================================
🧠 Auto-initializing GraphRAG for schema 'test_schema'...
✅ GraphRAG auto-initialized successfully (5.2s)
📊 Indexed 337 vectors (3 tables)
✅ PASS - Background GraphRAG Init: Initialized successfully in 5.2s

============================================================
TEST SUMMARY
============================================================
✅ Connection Fingerprinting: Generated valid fingerprint: a7f3b2c194e8d7f6
✅ Session State Clearing: All state cleared successfully
✅ Connection Change Detection: Consistent fingerprints for same connection
✅ Environment Variable Control: AUTO_GRAPHRAG=enabled
✅ Background GraphRAG Init: Initialized successfully in 5.2s

------------------------------------------------------------
TOTAL: 5/5 tests passed (100%)
------------------------------------------------------------
🎉 ALL TESTS PASSED! Phase 1 implementation is working correctly.
```

---

## Troubleshooting

### Issue: "No module named 'sqlalchemy'"
**Solution:** Run with `uv run python test_phase1_auto_graphrag.py`

### Issue: "Failed to connect to database"
**Solution:** Check database configuration in `.env` file

### Issue: "No tables analyzed"
**Solution:** Ensure database has tables in the specified schema

### Issue: GraphRAG initialization fails
**Solution:** Check logs for specific error, may need to install missing dependencies

### Issue: Test hangs
**Solution:** Ctrl+C to stop, check if database is accessible

---

## What Gets Tested

### 1. Connection Fingerprinting ✅
- Generates unique 16-character hash from connection params
- Hash format: SHA256 of `{db_type}://{host}:{port}/{database}@{schema}`
- Verifies fingerprint is consistent for same connection

### 2. Session State Clearing ✅
- Clears all cached data:
  - `schema_file`
  - `ontology_file`
  - `_cached_schema`
  - `loaded_ontology`
  - `graphrag_manager`
  - `graphrag_initialized`
- Verifies all fields set to None/False

### 3. Connection Change Detection ✅
- Detects when database connection changes
- Same connection = same fingerprint
- Different connection = different fingerprint
- Triggers automatic state clearing

### 4. Background GraphRAG Initialization ✅
- Starts async task without blocking
- Initializes with TF-IDF embeddings
- Builds vector index for tables/columns
- Sets `session.graphrag_initialized = True`
- Saves state to disk
- Logs progress and statistics

### 5. Environment Variable Control ✅
- Reads `AUTO_GRAPHRAG` from environment
- Defaults to `true` if not set
- Respects `false` to disable auto-init
- Proper boolean parsing

---

## Success Criteria

✅ **All 5 tests pass**
✅ **GraphRAG initializes in 5-15 seconds**
✅ **No errors in logs**
✅ **Background task doesn't block main operation**
✅ **Connection changes clear old state**

---

## Next Steps After Testing

If all tests pass:
1. ✅ Phase 1 is ready for production use
2. 📝 Document in release notes
3. 🚀 Deploy to users
4. 📊 Monitor performance and errors
5. 🎯 Plan Phase 2 (auto ontology generation)

If tests fail:
1. Review error messages in logs
2. Check database connectivity
3. Verify environment configuration
4. Review implementation for bugs
5. Fix and re-test

---

## Performance Benchmarks

Expected performance for Phase 1:

| Metric | Target | Typical |
|--------|--------|---------|
| Connection fingerprint | <1ms | <1ms |
| State clearing | <10ms | <5ms |
| Background GraphRAG init (10 tables) | 5-10s | 7s |
| Background GraphRAG init (100 tables) | 15-30s | 22s |
| Overhead on analyze_schema | <50ms | <20ms |

---

## Log Messages to Look For

### Success Indicators:
```
🔗 Initial connection established: <fingerprint>...
🔄 GraphRAG auto-initialization started in background
🧠 Auto-initializing GraphRAG for schema '<name>'...
✅ GraphRAG auto-initialized successfully (X.Xs)
📊 Indexed N vectors (M tables)
```

### Connection Change:
```
🔄 Connection changed (old: <id1>..., new: <id2>...)
🧹 Clearing session state (connection change)
✅ Session state cleared
```

### Error Indicators:
```
❌ GraphRAG auto-initialization failed: <error>
⚠️  GraphRAG not available, using basic validation
```

---

## Manual Verification Checklist

- [ ] AUTO_GRAPHRAG=true in .env
- [ ] Database credentials configured
- [ ] Server starts without errors
- [ ] connect_database logs "Initial connection established"
- [ ] analyze_schema logs "GraphRAG auto-initialization started"
- [ ] GraphRAG completes within 15 seconds
- [ ] graphrag_search works without explicit init
- [ ] Connection change clears state
- [ ] AUTO_GRAPHRAG=false disables auto-init

---

**Date:** 2026-02-27
**Version:** Phase 1 Implementation
**Status:** Ready for Testing
