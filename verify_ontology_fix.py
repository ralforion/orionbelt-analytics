#!/usr/bin/env python3
"""
Simple verification that the ontology workflow fix is in place.
No dependencies required - just checks the code structure.
"""

import sys
from pathlib import Path

# Colors
GREEN = '\033[92m'
RED = '\033[91m'
YELLOW = '\033[93m'
RESET = '\033[0m'

def test(name, condition, message=""):
    """Run a test and print result."""
    if condition:
        print(f"{GREEN}✓{RESET} {name}")
        return True
    else:
        print(f"{RED}✗{RESET} {name}")
        if message:
            print(f"  {YELLOW}{message}{RESET}")
        return False


def main():
    """Verify the fix is in place."""
    project_root = Path(__file__).parent
    passed = 0
    failed = 0

    print("\n" + "="*60)
    print("Ontology Workflow Fix Verification")
    print("="*60 + "\n")

    # Read main.py
    main_py = project_root / "src" / "main.py"
    if not main_py.exists():
        print(f"{RED}✗ Cannot find src/main.py{RESET}")
        return 1

    content = main_py.read_text()

    # Test 1: Lightweight mode caches TableInfo objects
    if test(
        "Lightweight mode caches table_info_objects",
        "table_info_objects = []" in content and
        "table_info_objects.append(table_info)" in content and
        "# LIGHTWEIGHT MODE" in content
    ):
        passed += 1
    else:
        failed += 1
        print(f"  {YELLOW}Lightweight mode must collect table_info_objects{RESET}")

    # Test 2: Lightweight mode calls cache_schema_analysis
    lightweight_start = content.find("# LIGHTWEIGHT MODE")
    if lightweight_start > 0:
        # Find the return statement in lightweight mode
        lightweight_section = content[lightweight_start:lightweight_start + 5000]

        has_cache_call = "session.cache_schema_analysis" in lightweight_section
        cache_before_return = lightweight_section.find("session.cache_schema_analysis") < lightweight_section.find("return lightweight_result")

        if test(
            "Lightweight mode calls session.cache_schema_analysis()",
            has_cache_call and cache_before_return,
            "Must cache before returning in lightweight mode"
        ):
            passed += 1
        else:
            failed += 1
    else:
        failed += 1
        print(f"{RED}✗ Could not find LIGHTWEIGHT MODE section{RESET}")

    # Test 3: Cache call passes table_info_objects
    if lightweight_start > 0:
        lightweight_section = content[lightweight_start:lightweight_start + 5000]

        if test(
            "Cache call passes table_info_objects list",
            'cache_schema_analysis(schema_name or "", table_info_objects)' in lightweight_section,
            "Must pass full TableInfo objects to cache"
        ):
            passed += 1
        else:
            failed += 1
    else:
        failed += 1

    # Test 4: Lightweight result includes next_step guidance
    if test(
        "Lightweight result guides to generate_ontology()",
        '"next_step": "generate_ontology"' in content and
        '"cache_hint"' in content,
        "Should tell user to call generate_ontology() next"
    ):
        passed += 1
    else:
        failed += 1

    # Test 5: generate_ontology still checks cached schema
    if test(
        "generate_ontology() uses cached schema",
        "cached_tables = session.get_cached_schema" in content and
        "if cached_tables:" in content,
        "generate_ontology must try to use cached data first"
    ):
        passed += 1
    else:
        failed += 1

    # Test 6: Verify the fix includes logging
    if lightweight_start > 0:
        lightweight_section = content[lightweight_start:lightweight_start + 5000]

        if test(
            "Lightweight mode logs caching action",
            "Cached" in lightweight_section and "generate_ontology" in lightweight_section,
            "Should log that data is cached for ontology generation"
        ):
            passed += 1
        else:
            failed += 1
    else:
        failed += 1

    # Summary
    print("\n" + "="*60)
    print(f"Results: {GREEN}{passed} passed{RESET}, {RED}{failed} failed{RESET}")
    print("="*60 + "\n")

    if failed == 0:
        print(f"{GREEN}✓ Ontology workflow fix verified!{RESET}")
        print(f"{GREEN}  Lightweight mode now caches full TableInfo for generate_ontology(){RESET}\n")
        return 0
    else:
        print(f"{RED}✗ Fix incomplete. Please review the changes.{RESET}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
