#!/usr/bin/env python3
"""Verification script to check which code location is being executed."""

import sys
from pathlib import Path

print("=" * 60)
print("CODE LOCATION VERIFICATION")
print("=" * 60)
print(f"Script location: {Path(__file__).parent.absolute()}")
print(f"Python executable: {sys.executable}")
print(f"Python version: {sys.version.split()[0]}")
print()

# Check for version file
version_file = Path(__file__).parent / "src" / "__init__.py"
if version_file.exists():
    content = version_file.read_text()
    for line in content.split('\n'):
        if '__version__' in line and '=' in line:
            print(f"Version line: {line.strip()}")
            break
else:
    print(f"❌ src/__init__.py not found at: {version_file}")

# Check schema.py for diagnostic logging
schema_file = Path(__file__).parent / "src" / "handlers" / "schema.py"
print(f"\nschema.py location: {schema_file.absolute()}")
print(f"schema.py exists: {schema_file.exists()}")

if schema_file.exists():
    content = schema_file.read_text()

    # Check for diagnostic logging markers
    markers = [
        "🔍 analyze_schema() called",
        "🔍 GraphRAG auto-init check (CACHED path):",
        "🔍 GraphRAG auto-init check (NON-CACHED path):"
    ]

    print("\nDiagnostic logging checks:")
    for marker in markers:
        found = marker in content
        status = "✅" if found else "❌"
        print(f"  {status} {marker}")

    all_found = all(marker in content for marker in markers)

    print()
    if all_found:
        print("✅ ALL DIAGNOSTIC LOGGING IS PRESENT IN THIS CODE")
        print("   If you don't see it in server logs, the server is running")
        print("   from a DIFFERENT location than this directory!")
    else:
        print("❌ DIAGNOSTIC LOGGING NOT FOUND OR INCOMPLETE")
        print("   This code doesn't have the latest changes!")
        print("   Try: git pull")
else:
    print("\n❌ schema.py not found!")

print()
print("=" * 60)
print("To use this code for your MCP server:")
print(f"  cd {Path(__file__).parent.absolute()}")
print("  python3 server.py")
print("=" * 60)
