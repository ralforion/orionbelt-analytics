"""One validator serves a session's queries, so no query state may live on it.

A CTE is a table the query defines for itself, recorded per *reference* -- the
same name can be a CTE in one scope and a real table in another. That record
used to sit on the validator and be reset at the top of `validate`, which is
only safe while one validation runs at a time. It travels with the query now.

These tests pin the property directly: the same validator, validating in several
threads at once, must return what it returns alone.
"""

import asyncio
import threading

import pytest

from src.obqc_validator import OBQCValidator
from tests.test_obqc_validator import create_sample_ontology_graph

# A CTE named after a real table, selecting a column only the CTE has. It is
# valid exactly while the CTE record survives the whole validation: lose it and
# `amount` is checked against the real `orders` table and a correct query is
# blocked. Beside it, the same name used only as a table, where a stale CTE
# record would excuse a column that does not exist.
WITH_CTE = (
    "WITH orders AS (SELECT total AS amount FROM orders) SELECT amount FROM orders"
)
NO_CTE_BAD_COLUMN = "SELECT nosuchcolumn FROM orders"
NO_CTE_GOOD = "SELECT total FROM orders"


@pytest.fixture
def validator() -> OBQCValidator:
    graph, base_uri = create_sample_ontology_graph()
    instance = OBQCValidator()
    instance.load_ontology(graph, base_uri)
    return instance


def _findings(validator: OBQCValidator, sql: str) -> list[str]:
    return [issue.message for issue in validator.validate(sql).issues]


class TestNoStateSurvivesAValidation:
    """What one query says about its CTEs must not outlive it."""

    def test_a_cte_does_not_excuse_the_next_query(self, validator):
        alone = _findings(validator, NO_CTE_BAD_COLUMN)
        assert any("nosuchcolumn" in message for message in alone)

        assert validator.validate(WITH_CTE).is_valid

        assert _findings(validator, NO_CTE_BAD_COLUMN) == alone

    def test_nothing_is_left_on_the_validator(self, validator):
        validator.validate(WITH_CTE)

        assert not hasattr(validator, "_cte_references")

    def test_a_failed_parse_leaves_nothing_behind(self, validator):
        expected = _findings(validator, NO_CTE_BAD_COLUMN)

        validator.validate("SELECT FROM WHERE ((")

        assert _findings(validator, NO_CTE_BAD_COLUMN) == expected


class TestConcurrentValidationsDoNotMix:
    """Validation is re-entrant: the same validator, several threads."""

    async def test_interleaved_validations_match_their_solo_results(self, validator):
        expected = {
            WITH_CTE: _findings(validator, WITH_CTE),
            NO_CTE_BAD_COLUMN: _findings(validator, NO_CTE_BAD_COLUMN),
            NO_CTE_GOOD: _findings(validator, NO_CTE_GOOD),
        }
        queries = [WITH_CTE, NO_CTE_BAD_COLUMN, NO_CTE_GOOD] * 8

        results = await asyncio.gather(
            *(asyncio.to_thread(_findings, validator, sql) for sql in queries)
        )

        for sql, findings in zip(queries, results, strict=True):
            assert findings == expected[sql], sql

    def test_a_validation_that_starts_mid_flight_changes_nothing(
        self, validator, monkeypatch
    ):
        """The race, made deterministic.

        Threads decide for themselves when to switch, so this hands control over
        at the exact point that used to matter: a second validation begins after
        the first has recorded its CTEs and before the rules read them. Held on
        the validator, the second one's reset wiped the first one's record and a
        correct query was reported as selecting a column that does not exist.
        """
        original = validator._extract_tables
        nested: list[bool] = []

        def interleave(*args, **kwargs):
            if not nested:
                nested.append(True)
                validator.validate(NO_CTE_BAD_COLUMN)
            return original(*args, **kwargs)

        monkeypatch.setattr(validator, "_extract_tables", interleave)

        result = validator.validate(WITH_CTE)

        assert nested == [True], "the interleaved validation never ran"
        assert result.is_valid, [issue.message for issue in result.issues]

    def test_many_threads_on_one_validator(self, validator):
        expected = _findings(validator, NO_CTE_BAD_COLUMN)
        mismatches: list[list[str]] = []
        barrier = threading.Barrier(8)

        def run() -> None:
            barrier.wait(timeout=10)
            for _ in range(20):
                # Valid only while this query's own CTE record stands.
                if not validator.validate(WITH_CTE).is_valid:
                    mismatches.append(_findings(validator, WITH_CTE))
                found = _findings(validator, NO_CTE_BAD_COLUMN)
                if found != expected:
                    mismatches.append(found)

        threads = [threading.Thread(target=run) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert mismatches == []
