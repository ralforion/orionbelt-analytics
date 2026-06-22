"""Tests for directed grain reachability (Phase 3): reachable_from / measurable_from.

Schema (edges point finer grain -> coarser grain, i.e. many-to-one):

    order_items --> orders --> customers
    returns --------------------^

So:
  - reachable_from (descendants, dimension-capable) walks toward customers
  - measurable_from (ancestors, measure-capable) walks toward order_items/returns
"""

from src.graphrag.retriever import GraphRetriever


def _tbl(name, fks=None):
    return {
        "name": name,
        "schema": "public",
        "columns": [],
        "primary_keys": ["id"],
        "foreign_keys": fks or [],
    }


def _build():
    tables = [
        _tbl("customers"),
        _tbl(
            "orders",
            [
                {
                    "column": "customer_id",
                    "referenced_table": "customers",
                    "referenced_column": "id",
                }
            ],
        ),
        _tbl(
            "order_items",
            [
                {
                    "column": "order_id",
                    "referenced_table": "orders",
                    "referenced_column": "id",
                }
            ],
        ),
        _tbl(
            "returns",
            [
                {
                    "column": "customer_id",
                    "referenced_table": "customers",
                    "referenced_column": "id",
                }
            ],
        ),
    ]
    r = GraphRetriever()
    r.build_graph(tables)
    return r


def test_reachable_from_walks_to_coarser_grain():
    r = _build()
    result = r.reachable_from("order_items")
    assert result["exists"] is True
    assert set(result["tables"]) == {"orders", "customers"}


def test_reachable_from_interior_fact():
    r = _build()
    # orders reaches only customers (not back down to order_items)
    assert set(r.reachable_from("orders")["tables"]) == {"customers"}


def test_reachable_from_dimension_is_empty():
    r = _build()
    # customers is a pure sink — nothing coarser
    assert r.reachable_from("customers")["tables"] == []


def test_measurable_from_walks_to_finer_grain():
    r = _build()
    # everything that fans out customers
    assert set(r.measurable_from("customers")["tables"]) == {
        "orders",
        "order_items",
        "returns",
    }


def test_measurable_from_interior_fact():
    r = _build()
    assert set(r.measurable_from("orders")["tables"]) == {"order_items"}


def test_reachable_and_measurable_are_inverses():
    r = _build()
    nodes = list(r.graph.nodes())
    for x in nodes:
        for y in r.reachable_from(x)["tables"]:
            # X reachable_from Y  <=>  Y measurable_from X
            assert x in r.measurable_from(y)["tables"]


def test_max_hops_bounds_closure():
    r = _build()
    one_hop = r.reachable_from("order_items", max_hops=1)
    assert set(one_hop["tables"]) == {"orders"}


def test_unknown_table():
    r = _build()
    assert r.reachable_from("nope")["exists"] is False
    assert r.measurable_from("nope")["exists"] is False


def test_cycle_terminates():
    # Mutual FKs a <-> b must not loop forever.
    tables = [
        _tbl(
            "a",
            [{"column": "b_id", "referenced_table": "b", "referenced_column": "id"}],
        ),
        _tbl(
            "b",
            [{"column": "a_id", "referenced_table": "a", "referenced_column": "id"}],
        ),
    ]
    r = GraphRetriever()
    r.build_graph(tables)
    # Closures are cycle-safe (visited set); the other node is reached exactly once.
    assert set(r.reachable_from("a")["tables"]) == {"b"}
    assert set(r.measurable_from("a")["tables"]) == {"b"}


def test_self_reference_terminates():
    tables = [
        _tbl(
            "employee",
            [
                {
                    "column": "manager_id",
                    "referenced_table": "employee",
                    "referenced_column": "id",
                }
            ],
        )
    ]
    r = GraphRetriever()
    r.build_graph(tables)
    # A self-loop reaches nothing new beyond itself (anchor excluded from results).
    assert r.reachable_from("employee")["tables"] == []
