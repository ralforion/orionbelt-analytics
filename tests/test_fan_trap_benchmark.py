"""The fan-trap benchmark's numbers, as docs/fan-trap-prevention.md states them.

scripts/bench_fan_traps.py runs queries that name only real tables and columns
through OBQC and through DuckDB, and calls a query wrong when its numbers
differ from a correct query's. These tests keep its cases honest -- each still
shows on the data what it claims -- and pin the results the documentation
quotes, so a change to OBQC that moves them has to say so.
"""

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import dotenv.main
import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "bench_fan_traps.py"


@pytest.fixture(scope="module")
def bench() -> ModuleType:
    spec = importlib.util.spec_from_file_location("bench_fan_traps", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def observed(
    bench: ModuleType, tmp_path_factory: pytest.TempPathFactory
) -> dict[str, Any]:
    """One run, in a shell that would leak into it if it could.

    A home directory of its own, a master password that makes every
    credential manager the server builds keep a salt there, and a note of
    every .env that python-dotenv is asked to load.
    """
    home = tmp_path_factory.mktemp("home")
    loads: list[Any] = []
    real_init = dotenv.main.DotEnv.__init__

    def spy(self: Any, *args: Any, **kwargs: Any) -> None:
        loads.append(kwargs.get("dotenv_path", args[0] if args else None))
        real_init(self, *args, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("HOME", str(home))
        mp.setenv("USERPROFILE", str(home))
        mp.setenv("MCP_MASTER_PASSWORD", "not-for-a-benchmark")
        mp.setattr(dotenv.main.DotEnv, "__init__", spy)
        outcome = bench.run()
        environment = dict(os.environ)
    return {
        "outcome": outcome,
        "dotenv_loads": loads,
        "home": sorted(p.name for p in home.iterdir()),
        "environment": environment,
    }


@pytest.fixture(scope="module")
def outcome(observed: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = observed["outcome"]
    return result


def test_it_reads_no_env_file_and_writes_nothing_home(
    observed: dict[str, Any],
) -> None:
    assert observed["dotenv_loads"] == []
    assert observed["home"] == []
    # And it hands the environment back as it found it.
    assert observed["environment"]["MCP_MASTER_PASSWORD"] == "not-for-a-benchmark"
    assert "PYTHON_DOTENV_DISABLED" not in observed["environment"]


def test_an_answer_that_merges_rows_has_no_percentage(bench: ModuleType) -> None:
    # Two students named Kim Lee, 3 and 8 credits; merged by name, 11. No one
    # number is what 11 should have been, so the answer is off by its rows.
    assert bench._error([("Kim Lee", 11)], [("Kim Lee", 3), ("Kim Lee", 8)]) is None
    assert bench._error([("a", 1), ("a", 2)], [("a", 1)]) is None
    # Paired rows still say by how much; rows only on one side are not paired.
    assert bench._error([("a", 12), ("b", 5)], [("a", 10), ("b", 5), ("c", 1)]) == (
        pytest.approx(0.2)
    )
    assert bench._error([(12,)], [(10,)]) == pytest.approx(0.2)
    assert bench._error([(12,), (5,)], [(10,), (5,)]) is None


def test_the_case_that_merges_two_students_reports_rows(
    outcome: dict[str, Any],
) -> None:
    (case,) = [c for c in outcome["cases"] if c["id"] == "credits-per-student-name"]
    assert case["label"] == "wrong"
    assert case["error"] is None


def _ids(outcome: dict[str, Any], label: str, scenario: str, verdict: str) -> set[str]:
    return {
        c["id"]
        for c in outcome["cases"]
        if c["label"] == label and c["outcome"][scenario] == verdict
    }


def test_every_case_shows_on_the_data_what_it_claims(outcome: dict[str, Any]) -> None:
    assert [c["id"] for c in outcome["cases"] if c["label"] != c["expect"]] == []
    wrong = [c for c in outcome["cases"] if c["label"] == "wrong"]
    assert len(wrong) == 12
    # Wrong by a margin, not by rounding.
    assert all(c["error"] is None or c["error"] >= 0.05 for c in wrong)


def test_a_check_of_names_lets_every_wrong_number_through(
    outcome: dict[str, Any],
) -> None:
    assert outcome["summary"]["names only"] == {
        "wrong": {"blocked": 0, "warned": 0, "passed": 12},
        "right": {"blocked": 0, "warned": 0, "passed": 17},
    }


def test_the_numbers_the_documentation_quotes(outcome: dict[str, Any]) -> None:
    firm = {
        "wrong": {"blocked": 10, "warned": 0, "passed": 2},
        "right": {"blocked": 1, "warned": 0, "passed": 16},
    }
    assert outcome["summary"]["declared"] == firm
    assert outcome["summary"]["inferred + checked"] == firm
    assert outcome["summary"]["inferred"] == {
        "wrong": {"blocked": 0, "warned": 10, "passed": 2},
        "right": {"blocked": 0, "warned": 1, "passed": 16},
    }
    assert outcome["verdicts"] == {"confirmed": 8}


def test_the_misses_and_the_false_alarm_are_the_documented_ones(
    outcome: dict[str, Any],
) -> None:
    for scenario in ("declared", "inferred", "inferred + checked"):
        assert _ids(outcome, "wrong", scenario, "passed") == {
            "sum-distinct-total-per-customer",
            "orders-with-books-count-star",
        }, scenario
    assert _ids(outcome, "right", "declared", "blocked") == {
        "order-value-and-shipping-per-customer"
    }
