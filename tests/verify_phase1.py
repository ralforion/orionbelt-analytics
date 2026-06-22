#!/usr/bin/env python3
"""
Simple verification script for Phase 1 changes.
No dependencies required - just standard library.
"""

import sys
from pathlib import Path

# Colors for output
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
RESET = "\033[0m"


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
    """Run all verification tests."""
    project_root = Path(__file__).parent.parent
    passed = 0
    failed = 0

    print("\n" + "=" * 60)
    print("Phase 1 Verification Tests")
    print("=" * 60 + "\n")

    # Test 1: Skills directory exists
    skills_dir = project_root / ".claude" / "skills"
    if test("Skills directory exists", skills_dir.exists() and skills_dir.is_dir()):
        passed += 1
    else:
        failed += 1

    # Test 2: All 3 skills present
    expected_skills = [
        "fan-trap-prevention.md",
        "sql-best-practices.md",
        "chart-examples.md",
    ]

    all_skills_present = True
    for skill in expected_skills:
        skill_path = skills_dir / skill
        if not skill_path.exists():
            all_skills_present = False
            print(f"  {YELLOW}Missing: {skill}{RESET}")

    if test("All 3 skills present", all_skills_present):
        passed += 1
    else:
        failed += 1

    # Test 3: Skills have content
    skills_large_enough = True
    for skill in expected_skills:
        skill_path = skills_dir / skill
        if skill_path.exists() and skill_path.stat().st_size < 1000:
            skills_large_enough = False
            print(
                f"  {YELLOW}{skill} too small: {skill_path.stat().st_size} bytes{RESET}"
            )

    if test("All skills have sufficient content", skills_large_enough):
        passed += 1
    else:
        failed += 1

    # Test 4: Fan-trap skill content
    fan_trap_path = skills_dir / "fan-trap-prevention.md"
    if fan_trap_path.exists():
        content = fan_trap_path.read_text()
        has_key_content = (
            "What is a Fan-Trap?" in content
            and "UNION ALL" in content
            and "Detection Checklist" in content
        )
        if test("Fan-trap skill has key content", has_key_content):
            passed += 1
        else:
            failed += 1
    else:
        failed += 1

    # Test 5: SQL best practices skill content
    sql_path = skills_dir / "sql-best-practices.md"
    if sql_path.exists():
        content = sql_path.read_text()
        has_key_content = (
            "Identifier Qualification" in content and "schema.table.column" in content
        )
        if test("SQL best practices skill has key content", has_key_content):
            passed += 1
        else:
            failed += 1
    else:
        failed += 1

    # Test 6: Chart examples skill content
    chart_path = skills_dir / "chart-examples.md"
    if chart_path.exists():
        content = chart_path.read_text()
        has_key_content = (
            "bar" in content.lower()
            and "line" in content.lower()
            and "scatter" in content.lower()
            and "generate_chart" in content
        )
        if test("Chart examples skill has key content", has_key_content):
            passed += 1
        else:
            failed += 1
    else:
        failed += 1

    # Test 7: main.py exists and is condensed
    main_py = project_root / "src" / "main.py"
    if test("main.py exists", main_py.exists()):
        passed += 1

        # Count lines
        with open(main_py) as f:
            line_count = sum(1 for _ in f)

        if test(f"main.py condensed ({line_count} lines)", line_count < 2200):
            passed += 1
        else:
            failed += 1
            print(f"  {YELLOW}Expected < 2200 lines, got {line_count}{RESET}")
    else:
        failed += 2

    # Test 8: Backup exists
    backup = project_root / "src" / "main.py.backup"
    if test("Backup file exists", backup.exists()):
        passed += 1
    else:
        failed += 1

    # Test 9: Server instructions condensed
    if main_py.exists():
        content = main_py.read_text()
        start = content.find('instructions="""')
        if start != -1:
            end = content.find('"""', start + 20)
            instructions = content[start:end]
            instructions_condensed = len(instructions) < 2500

            if test("Server instructions condensed", instructions_condensed):
                passed += 1
                print(f"  {GREEN}Instructions: {len(instructions)} chars{RESET}")
            else:
                failed += 1
                print(
                    f"  {YELLOW}Instructions: {len(instructions)} chars (expected < 2000){RESET}"
                )

            # Check for skill references
            has_skill_refs = (
                "/fan-trap-prevention" in instructions
                and "/sql-best-practices" in instructions
            )
            if test("Server instructions reference skills", has_skill_refs):
                passed += 1
            else:
                failed += 1
        else:
            failed += 2
            print(f"  {YELLOW}Could not find instructions block{RESET}")
    else:
        failed += 2

    # Test 10: Token savings estimate
    if main_py.exists() and backup.exists():
        new_content = main_py.read_text()
        old_content = backup.read_text()

        # Rough token estimate (chars / 4)
        old_tokens = len(old_content) // 4
        new_tokens = len(new_content) // 4
        savings = old_tokens - new_tokens

        if test(f"Token savings achieved ({savings:,} tokens)", savings >= 6000):
            passed += 1
            print(f"  {GREEN}Saved approximately {savings:,} tokens{RESET}")
        else:
            failed += 1
            print(f"  {YELLOW}Only saved {savings:,} tokens (expected >= 6000){RESET}")
    else:
        failed += 1

    # Summary
    print("\n" + "=" * 60)
    print(f"Results: {GREEN}{passed} passed{RESET}, {RED}{failed} failed{RESET}")
    print("=" * 60 + "\n")

    if failed == 0:
        print(f"{GREEN}✓ All tests passed! Phase 1 deployment successful.{RESET}\n")
        return 0
    else:
        print(f"{RED}✗ Some tests failed. Please review the output above.{RESET}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
