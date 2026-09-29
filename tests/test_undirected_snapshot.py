"""The undirected graph is reused between lookups and dropped when it changes.

Join lookups read the graph undirected, because a path may cross a foreign key
against its direction. Converting per lookup cost ~1 ms on 400 tables and
~2.6 ms on 1,000. These tests pin the two halves of reusing one conversion:
it is shared while the graph stands still, and it is gone the moment the graph
changes -- a stale topology would offer joins the schema no longer has.
"""

import contextlib
from typing import Any

from src.graphrag.retriever import GraphRetriever


def _star(dimensions: int, schema: str = "sales") -> list[dict[str, Any]]:
    """A fact table referencing N dimensions, all joinable through it."""
    fact = {
        "name": "fact",
        "schema": schema,
        "columns": [{"name": "id", "type": "INT"}],
        "foreign_keys": [
            {
                "column": f"d{i}_id",
                "referenced_table": f"dim{i}",
                "referenced_column": "id",
            }
            for i in range(dimensions)
        ],
    }
    dims = [
        {
            "name": f"dim{i}",
            "schema": schema,
            "columns": [{"name": "id", "type": "INT"}],
            "foreign_keys": [],
        }
        for i in range(dimensions)
    ]
    return [fact, *dims]


class TestSnapshotIsReused:
    """One conversion serves every lookup until the graph changes."""

    def test_two_lookups_share_one_conversion(self) -> None:
        retriever = GraphRetriever()
        retriever.build_graph(_star(3))

        first = retriever._undirected_snapshot()
        second = retriever._undirected_snapshot()

        assert first is second

    def test_join_lookups_do_not_convert_again(self) -> None:
        retriever = GraphRetriever()
        retriever.build_graph(_star(3))

        retriever.find_join_path("dim0", "dim1")
        after_first = retriever._undirected

        retriever.find_join_path("dim0", "dim2")

        assert retriever._undirected is after_first

    def test_the_snapshot_holds_the_same_topology(self) -> None:
        retriever = GraphRetriever()
        retriever.build_graph(_star(3))

        snapshot = retriever._undirected_snapshot()

        assert set(snapshot.nodes) == set(retriever.graph.nodes)
        assert snapshot.number_of_edges() == retriever.graph.number_of_edges()
        # Undirected: reachable against the foreign key's direction.
        assert snapshot.has_edge("dim0", "fact")


class TestSnapshotIsDropped:
    """Any change to the graph invalidates what was taken from it."""

    def test_building_again_drops_it(self) -> None:
        retriever = GraphRetriever()
        retriever.build_graph(_star(3))
        stale = retriever._undirected_snapshot()

        retriever.build_graph(_star(2))
        fresh = retriever._undirected_snapshot()

        assert fresh is not stale
        assert "dim2" not in fresh

    def test_adding_tables_drops_it(self) -> None:
        retriever = GraphRetriever()
        retriever.build_graph(_star(2))
        stale = retriever._undirected_snapshot()

        retriever.add_to_graph(
            [
                {
                    "name": "extra",
                    "schema": "sales",
                    "columns": [{"name": "id", "type": "INT"}],
                    "foreign_keys": [
                        {
                            "column": "fact_id",
                            "referenced_table": "fact",
                            "referenced_column": "id",
                        }
                    ],
                }
            ]
        )
        fresh = retriever._undirected_snapshot()

        assert fresh is not stale
        assert fresh.has_edge("extra", "fact")

    def test_a_dropped_foreign_key_stops_offering_the_join(self) -> None:
        """The reason invalidation matters: a removed edge must disappear."""
        retriever = GraphRetriever()
        retriever.build_graph(_star(2))
        assert retriever.find_join_path("dim0", "dim1") is not None

        without_keys = _star(2)
        without_keys[0]["foreign_keys"] = []
        retriever.add_to_graph(without_keys)

        assert retriever.find_join_path("dim0", "dim1") is None

    def test_a_failed_build_leaves_no_snapshot(self) -> None:
        retriever = GraphRetriever()
        retriever.build_graph(_star(2))
        retriever._undirected_snapshot()

        broken: list[dict[str, Any]] = [{"schema": "sales"}]  # no "name"
        with contextlib.suppress(KeyError):
            retriever.build_graph(broken)

        assert retriever._undirected is None


class TestResultsAreUnchanged:
    """Reusing the conversion returns exactly what converting each time did."""

    def test_join_paths_match_a_fresh_conversion(self) -> None:
        retriever = GraphRetriever()
        retriever.build_graph(_star(12))

        for target in range(1, 12):
            with_snapshot = retriever.find_join_path("dim0", f"dim{target}")
            retriever._undirected = None
            without_snapshot = retriever.find_join_path("dim0", f"dim{target}")
            assert with_snapshot == without_snapshot

    def test_alternative_paths_match_a_fresh_conversion(self) -> None:
        retriever = GraphRetriever()
        retriever.build_graph(_star(6))
        chosen = retriever.find_join_path("dim0", "dim1")
        assert chosen is not None

        with_snapshot = retriever.find_alternative_join_paths("dim0", "dim1", chosen)
        retriever._undirected = None
        without_snapshot = retriever.find_alternative_join_paths("dim0", "dim1", chosen)

        assert with_snapshot == without_snapshot
