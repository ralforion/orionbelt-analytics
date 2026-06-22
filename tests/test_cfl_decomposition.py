"""Tests for Phase 5 CFL (Composite Fact Layer) decomposition advice."""

from types import SimpleNamespace

from src.graphrag.retriever import GraphRetriever
from src.handler_context import HandlerContext
from src.handlers import graphrag as h


def _tbl(name, fks=None):
    return {
        "name": name,
        "schema": "public",
        "columns": [],
        "primary_keys": ["id"],
        "foreign_keys": fks or [],
    }


def _session():
    """A fake session exposing a real GraphRetriever via graphrag_manager."""
    tables = [
        _tbl("customers"),
        _tbl("products"),
        _tbl(
            "orders",
            [
                {
                    "column": "customer_id",
                    "referenced_table": "customers",
                    "referenced_column": "id",
                },
            ],
        ),
        _tbl(
            "order_items",
            [
                {
                    "column": "order_id",
                    "referenced_table": "orders",
                    "referenced_column": "id",
                },
                {
                    "column": "product_id",
                    "referenced_table": "products",
                    "referenced_column": "id",
                },
            ],
        ),
        _tbl(
            "returns",
            [
                {
                    "column": "customer_id",
                    "referenced_table": "customers",
                    "referenced_column": "id",
                },
            ],
        ),
    ]
    retriever = GraphRetriever()
    retriever.build_graph(tables)
    manager = SimpleNamespace(graph_retriever=retriever)
    return SimpleNamespace(graphrag_initialized=True, graphrag_manager=manager)


class _Ctx:
    async def info(self, *_a, **_k):
        return None


def _err(message, code):
    return {"success": False, "error": message, "code": code}


async def _call(facts, dimensions=None, session=None):
    sess = session or _session()
    return await h.plan_composite_query(
        _Ctx(),
        facts,
        dimensions,
        services=HandlerContext(
            get_session_data=lambda _ctx: sess,
            create_error_response=_err,
        ),
    )


async def test_two_disjoint_facts_require_cfl():
    res = await _call(["orders", "returns"])
    assert res["success"] is True
    assert res["cfl_required"] is True
    assert set(res["leg_roots"]) == {"orders", "returns"}
    # customers reachable from both -> conformed GROUP BY key
    assert "customers" in res["conformed_dimensions"]


async def test_legs_have_null_pad_for_unshared_dims():
    # order_items reaches products + orders + customers; returns reaches customers.
    res = await _call(["order_items", "returns"], dimensions=["customers", "products"])
    assert res["cfl_required"] is True
    legs = {leg["root"]: leg for leg in res["legs"]}
    # products reachable from order_items, not from returns -> null-padded in returns leg
    assert "products" in legs["order_items"]["dimensions"]
    assert "products" in legs["returns"]["null_pad"]
    # customers is conformed (reachable from both)
    assert "customers" in res["conformed_dimensions"]


async def test_same_grain_chain_is_not_cfl():
    # orders is reachable from order_items -> same grain chain, single leg root.
    res = await _call(["order_items", "orders"])
    assert res["cfl_required"] is False
    assert res["leg_roots"] == ["order_items"]


async def test_single_fact_not_cfl():
    res = await _call(["orders"])
    assert res["cfl_required"] is False


async def test_missing_table_errors():
    res = await _call(["nope"])
    assert res["success"] is False
    assert res["code"] == "data_error"


async def test_empty_facts_errors():
    res = await _call([])
    assert res["success"] is False
    assert res["code"] == "parameter_error"
