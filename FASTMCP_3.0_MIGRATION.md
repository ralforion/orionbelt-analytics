# FastMCP 3.0.2 Migration Guide

**Date:** 2026-02-28
**From:** FastMCP 2.14.5
**To:** FastMCP 3.0.2

## Summary

OrionBelt Analytics has been updated to use **FastMCP 3.0.2**, the latest stable release. The good news: **no code changes required** for our codebase! Our usage of FastMCP is fully compatible with v3.0.

## What Changed in pyproject.toml

```diff
dependencies = [
-   "fastmcp>=2.14.5",
+   "fastmcp>=3.0.2",
]
```

## Installation

After pulling the updated `pyproject.toml`, reinstall dependencies:

```bash
# Using pip
pip install --upgrade fastmcp

# Or reinstall all dependencies
pip install -e .

# Using uv (if available)
uv pip install --upgrade fastmcp
```

## Why No Code Changes?

FastMCP 3.0 maintains backward compatibility for the core APIs we use:

✅ **`FastMCP()` initialization** - We only use `name=` and `instructions=` parameters
✅ **`@mcp.tool()` decorator** - Fully compatible, no changes needed
✅ **`@mcp.resource()` decorator** - Fully compatible
✅ **No state management** - We don't use `ctx.get_state()` or `ctx.set_state()`
✅ **No UI configuration** - We don't use `ui=` or `app=` parameters
✅ **No visibility system** - We don't use `enabled=` parameters

## Breaking Changes in FastMCP 3.0 (Not Affecting Us)

These changes **DO NOT** affect OrionBelt Analytics:

### 1. Decorator Behavior (Not Used)
- **v2**: Decorators consumed functions (couldn't call decorated function directly)
- **v3**: Decorators return callable functions (can call them like normal functions)
- **Impact**: None - we don't call our tool functions directly

### 2. State Management API (Not Used)
- **v2**: Synchronous `ctx.get_state()`, `ctx.set_state()`
- **v3**: Asynchronous `await ctx.get_state()`, `await ctx.set_state()`
- **Impact**: None - we don't use state management

### 3. Component Visibility (Not Used)
- **v2**: `@mcp.tool(enabled=False)`
- **v3**: `mcp.disable(names={"tool_name"})`
- **Impact**: None - we don't disable tools programmatically

### 4. UI Configuration (Not Used)
- **v2**: `FastMCP(ui=...)`
- **v3**: `FastMCP(app=...)` with `AppConfig`
- **Impact**: None - we don't configure UI/app settings

## What's New in FastMCP 3.0

FastMCP 3.0 introduces a major architectural rebuild:

### Architecture
- **Providers**: FileSystemProvider, OpenAPIProvider, ProxyProvider, SkillsProvider
- **Transforms**: Rename, namespace, filter, version, secure
- **Session-scoped state**: State persists across requests

### Improvements
- More composable architecture
- Better performance
- Enhanced provider system for sourcing components

## Testing the Migration

After upgrading, test the following:

1. **Server Startup**
   ```bash
   python -m src.main
   ```

2. **Tool Discovery**
   - Verify all tools are registered correctly
   - Check `connect_database()`, `analyze_schema()`, `generate_ontology()`

3. **Resources**
   - Verify skills work: `/fan-trap-prevention`, `/sql-best-practices`, `/chart-examples`
   - Check chart viewer UI: `ui://orionbelt/chart-viewer`

4. **Core Workflows**
   - Test database connection
   - Test schema analysis
   - Test ontology generation
   - Test SQL execution

## Rollback Plan

If issues arise, rollback is simple:

```bash
# Revert pyproject.toml
git checkout pyproject.toml

# Reinstall dependencies
pip install -e .
```

Or manually downgrade:

```bash
pip install fastmcp==2.14.5
```

## References

- [FastMCP 3.0 Release Notes](https://www.jlowin.dev/blog/fastmcp-3)
- [What's New in FastMCP 3.0](https://www.jlowin.dev/blog/fastmcp-3-whats-new)
- [FastMCP on PyPI](https://pypi.org/project/fastmcp/)
- [FastMCP Updates](https://gofastmcp.com/updates)

## Changelog Entry

Added to `CHANGELOG_2026-02-27.md`:

### FastMCP 3.0.2 Upgrade (2026-02-28)

**Upgrade:** FastMCP 2.14.5 → 3.0.2

**Reason:** Stay current with latest MCP framework improvements

**Code Changes:** None required - full backward compatibility

**Files Modified:**
- `pyproject.toml` - Updated fastmcp dependency to >=3.0.2

**Testing:** Verify server startup, tool registration, and core workflows

**Documentation:** See `FASTMCP_3.0_MIGRATION.md`

---

## Next Steps

1. Install/upgrade FastMCP to 3.0.2
2. Run tests to verify compatibility
3. Explore new v3.0 features (providers, transforms) for future enhancements
