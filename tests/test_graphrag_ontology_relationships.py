"""Join discovery for a schema whose keys the database does not declare.

GraphRAG's join graph was built from declared foreign keys alone. On a layer
without them -- a Databricks lakehouse, ClickHouse, most of BigQuery -- every
join-path tool came back empty, although the generated ontology had found the
relationships from the column names and a user could load an ontology that
states them. Inferred relationships now go into the shared graph, marked as
inferred; a loaded ontology's go into a copy for that session alone.

Alongside: a loaded ontology was ignored by OBQC whenever a generated one also
existed, because the generated file was always read first.
"""

from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import src.server_state as server_state
from src.database_manager import ColumnInfo, TableInfo
from src.graphrag.retriever import GraphRetriever
from src.handler_context import HandlerContext
from src.handlers import graphrag as handler
from src.obqc_validator import OBQCValidator
from src.ontology_generator import OntologyGenerator

BASE = "http://example.com/ontology/"


def _col(name: str, data_type: str = "INTEGER", pk: bool = False) -> ColumnInfo:
    return ColumnInfo(
        name=name,
        data_type=data_type,
        is_nullable=not pk,
        is_primary_key=pk,
        is_foreign_key=False,
    )


def _info(
    name: str,
    columns: list[ColumnInfo],
    foreign_keys: list[dict[str, str]] | None = None,
    schema: str = "gold",
) -> TableInfo:
    return TableInfo(
        name=name,
        schema=schema,
        columns=columns,
        primary_keys=[c.name for c in columns if c.is_primary_key],
        foreign_keys=foreign_keys or [],
    )


def _keyless_schema() -> list[TableInfo]:
    """A layer with no declared keys, whose names say how it joins."""
    return [
        _info("customers", [_col("id", pk=True), _col("name", "VARCHAR")]),
        _info(
            "orders",
            [_col("id", pk=True), _col("customer_id"), _col("amount", "DECIMAL")],
        ),
        # A text column that merely carries a table's name: a weak guess.
        _info("notes", [_col("id", pk=True), _col("customer", "VARCHAR")]),
    ]


def _services(session: Any, **extra: Any) -> HandlerContext:
    return HandlerContext(
        get_session_data=lambda _ctx: session,
        create_error_response=lambda message, code=None, *rest: {
            "success": False,
            "error": message,
            "error_type": code,
        },
        **extra,
    )


def _session(retriever: GraphRetriever, **fields: Any) -> Any:
    session = SimpleNamespace(
        graphrag_initialized=True,
        graphrag_manager=SimpleNamespace(graph_retriever=retriever),
        current_schema="gold",
        loaded_ontology=None,
        ontology_join_graph=None,
    )
    for name, value in fields.items():
        setattr(session, name, value)
    return session


class TestInferredRelationshipsReachTheGraph:
    def test_a_named_key_becomes_an_inferred_edge(self):
        tables = handler._tables_to_dicts(_keyless_schema())
        orders = next(t for t in tables if t["name"] == "orders")

        assert orders["foreign_keys"] == [
            {
                "column": "customer_id",
                "referenced_table": "customers",
                "referenced_column": "id",
                "inferred": True,
                "confidence": "high",
            }
        ]

    def test_a_low_confidence_guess_is_left_out(self):
        tables = handler._tables_to_dicts(_keyless_schema())
        notes = next(t for t in tables if t["name"] == "notes")

        assert notes["foreign_keys"] == []

    def test_a_declared_key_is_not_inferred_again(self):
        schema = _keyless_schema()
        schema[1].foreign_keys = [
            {
                "column": "customer_id",
                "referenced_table": "customers",
                "referenced_column": "id",
            }
        ]
        tables = handler._tables_to_dicts(schema)
        orders = next(t for t in tables if t["name"] == "orders")

        assert len(orders["foreign_keys"]) == 1
        assert "inferred" not in orders["foreign_keys"][0]

    async def test_a_join_path_is_found_and_says_it_is_inferred(self):
        retriever = GraphRetriever()
        retriever.build_graph(handler._tables_to_dicts(_keyless_schema()))
        session = _session(retriever)

        result = await handler.graphrag_find_join_path(
            Mock(), "orders", "customers", 12, _services(session)
        )

        assert result["success"] is True
        assert result["joins"][0]["source"] == "inferred"
        assert result["joins"][0]["confidence"] == "high"
        assert "inferred_joins_note" in result


class TestDeclaredKeysOutrankInferredOnes:
    def _table(self, fks: list[dict[str, Any]]) -> dict[str, Any]:
        return {"name": "orders", "schema": "gold", "columns": [], "foreign_keys": fks}

    def _customers(self) -> dict[str, Any]:
        return {
            "name": "customers",
            "schema": "gold",
            "columns": [],
            "foreign_keys": [],
        }

    def test_an_inferred_key_does_not_replace_a_declared_one(self):
        declared = {
            "column": "buyer",
            "referenced_table": "customers",
            "referenced_column": "id",
        }
        inferred = {
            "column": "customer_id",
            "referenced_table": "customers",
            "referenced_column": "id",
            "inferred": True,
            "confidence": "medium",
        }
        retriever = GraphRetriever()
        retriever.build_graph([self._table([declared, inferred]), self._customers()])

        edge = retriever.graph["gold.orders"]["gold.customers"]
        assert edge["column"] == "buyer"
        assert edge["inferred"] is False

    def test_a_declared_key_replaces_an_inferred_one(self):
        inferred = {
            "column": "customer_id",
            "referenced_table": "customers",
            "referenced_column": "id",
            "inferred": True,
            "confidence": "medium",
        }
        declared = {
            "column": "buyer",
            "referenced_table": "customers",
            "referenced_column": "id",
        }
        retriever = GraphRetriever()
        retriever.build_graph([self._table([inferred, declared]), self._customers()])

        edge = retriever.graph["gold.orders"]["gold.customers"]
        assert edge["column"] == "buyer"
        assert edge["inferred"] is False
        assert edge["confidence"] is None
        joins = retriever.find_join_path("gold.orders", "gold.customers")
        assert joins is not None and "source" not in joins[0]


def _uploaded_ontology() -> str:
    """An ontology stating a join the names give no hint of."""
    tables = [
        _info("dim_client", [_col("id", pk=True), _col("label", "VARCHAR")]),
        _info(
            "fact_sales",
            [_col("id", pk=True), _col("cust_key"), _col("amt", "DECIMAL")],
            foreign_keys=[
                {
                    "column": "cust_key",
                    "referenced_table": "dim_client",
                    "referenced_column": "id",
                }
            ],
        ),
    ]
    generator = OntologyGenerator(BASE)
    generator.generate_from_schema(tables, include_inferred_relationships=False)
    return generator.serialize_ontology()


def _undeclared_graph() -> GraphRetriever:
    tables = [
        _info("dim_client", [_col("id", pk=True), _col("label", "VARCHAR")]),
        _info("fact_sales", [_col("id", pk=True), _col("cust_key"), _col("amt")]),
    ]
    retriever = GraphRetriever()
    retriever.build_graph(handler._tables_to_dicts(tables))
    return retriever


def _validator_for(ttl: str) -> OBQCValidator:
    generator = OntologyGenerator(BASE)
    generator.load_from_string(ttl)
    validator = OBQCValidator()
    validator.load_ontology(generator.graph, BASE)
    return validator


class TestALoadedOntologyExtendsJoinsForItsSessionOnly:
    def _services(self, session: Any, validator: OBQCValidator | None) -> Any:
        async def aget_validator(_ctx: Any) -> OBQCValidator | None:
            return validator

        return _services(session, aget_session_obqc_validator=aget_validator)

    async def test_the_session_that_loaded_it_finds_the_join(self):
        retriever = _undeclared_graph()
        assert retriever.find_join_path("fact_sales", "dim_client") is None
        ttl = _uploaded_ontology()
        session = _session(retriever, loaded_ontology=ttl)

        result = await handler.graphrag_find_join_path(
            Mock(),
            "fact_sales",
            "dim_client",
            12,
            self._services(session, _validator_for(ttl)),
        )

        assert result["success"] is True
        assert result["joins"][0]["from_column"] == "cust_key"
        assert result["joins"][0]["source"] == "ontology"

    async def test_another_session_on_the_connection_does_not(self):
        retriever = _undeclared_graph()
        ttl = _uploaded_ontology()
        mine = _session(retriever, loaded_ontology=ttl)
        theirs = _session(retriever)
        await handler._join_graph(
            Mock(), mine, self._services(mine, _validator_for(ttl))
        )

        result = await handler.graphrag_find_join_path(
            Mock(), "fact_sales", "dim_client", 12, self._services(theirs, None)
        )

        assert result["success"] is False
        assert retriever.find_join_path("fact_sales", "dim_client") is None

    async def test_the_copy_is_reused_until_the_graph_changes(self):
        retriever = _undeclared_graph()
        ttl = _uploaded_ontology()
        session = _session(retriever, loaded_ontology=ttl)
        services = self._services(session, _validator_for(ttl))

        first = await handler._join_graph(Mock(), session, services)
        again = await handler._join_graph(Mock(), session, services)
        retriever.add_to_graph(
            [{"name": "extra", "schema": "other", "columns": [], "foreign_keys": []}]
        )
        rebuilt = await handler._join_graph(Mock(), session, services)

        assert first is not retriever
        assert again is first
        assert rebuilt is not first
        assert "other.extra" in rebuilt.graph

    async def test_a_capability_tool_reads_the_copy(self):
        retriever = _undeclared_graph()
        ttl = _uploaded_ontology()
        session = _session(retriever, loaded_ontology=ttl)

        result = await handler.reachable_from(
            Mock(), "fact_sales", None, self._services(session, _validator_for(ttl))
        )

        assert result["success"] is True
        assert result["reachable_tables"] == ["gold.dim_client"]


class TestWithRelationships:
    def test_the_original_graph_is_untouched(self):
        retriever = _undeclared_graph()
        extended = retriever.with_relationships(
            [("gold.fact_sales", "cust_key", "gold.dim_client", "id")]
        )

        assert extended.graph.has_edge("gold.fact_sales", "gold.dim_client")
        assert not retriever.graph.has_edge("gold.fact_sales", "gold.dim_client")

    def test_nothing_new_returns_the_graph_itself(self):
        retriever = _undeclared_graph()

        assert retriever.with_relationships([]) is retriever
        assert (
            retriever.with_relationships([("gold.nope", "x", "gold.dim_client", "id")])
            is retriever
        )

    def test_an_existing_edge_is_kept(self):
        retriever = GraphRetriever()
        retriever.build_graph(handler._tables_to_dicts(_keyless_schema()))

        extended = retriever.with_relationships(
            [("gold.orders", "other_col", "gold.customers", "id")]
        )

        assert extended is retriever

    def test_an_ontology_name_in_another_case_is_matched(self):
        retriever = _undeclared_graph()

        assert retriever.indexed_name("FACT_SALES") == "fact_sales"
        assert retriever.indexed_name("missing") is None


class TestALoadedOntologyWinsOverTheGeneratedOne:
    def test_obqc_reads_the_loaded_ontology(self, tmp_path, monkeypatch):
        monkeypatch.setattr(server_state, "ensure_output_dir", lambda: tmp_path)
        (tmp_path / "ontology_gold.ttl").write_text("# generated", encoding="utf-8")
        session = SimpleNamespace(
            runtime=None,
            ontology_file="ontology_gold.ttl",
            loaded_ontology="# uploaded",
            loaded_ontology_path=None,
            connection_id=None,
        )

        key = server_state._ontology_revision_key(session, BASE)

        assert key is not None and key[0] == "text"

    def test_without_a_loaded_ontology_the_generated_one_is_read(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(server_state, "ensure_output_dir", lambda: tmp_path)
        (tmp_path / "ontology_gold.ttl").write_text("# generated", encoding="utf-8")
        session = SimpleNamespace(
            runtime=None,
            ontology_file="ontology_gold.ttl",
            loaded_ontology=None,
            loaded_ontology_path=None,
            connection_id=None,
        )

        key = server_state._ontology_revision_key(session, BASE)

        assert key is not None and key[0] == "file"


_UNMAPPED_TTL = (
    "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
    "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n"
    '<http://x/Customer> a owl:Class ; rdfs:label "Customer" .\n'
)


class TestAnUploadMustMapToTheDatabase:
    """Only an ontology OBQC can read replaces the active one."""

    def _session(self) -> Any:
        from src.session import SessionData

        session = SessionData()
        session.set_current_schema("gold")
        session.ontology_file = "ontology_gold.ttl"
        return session

    async def _load(self, session: Any, ttl: str, tmp_path: Any) -> dict[str, Any]:
        from src.handlers import ontology_io

        return await ontology_io.load_my_ontology(
            Mock(),
            str(tmp_path),
            False,
            None,
            HandlerContext(get_session_data=lambda _ctx: session),
            ontology_content=ttl,
            file_name="mine.ttl",
        )

    async def test_an_ontology_without_oba_mappings_is_not_activated(self, tmp_path):
        session = self._session()

        result = await self._load(session, _UNMAPPED_TTL, tmp_path)

        assert result["success"] is True
        assert result["activated"] is False
        assert result["oba_requirements"]["met"] is False
        assert "NOT activated" in result["note"]
        # The generated ontology stays the one OBQC reads.
        assert session.loaded_ontology is None
        assert session.ontology_file == "ontology_gold.ttl"

    async def test_it_does_not_displace_an_earlier_upload_either(self, tmp_path):
        session = self._session()
        mapped = _uploaded_ontology()
        await self._load(session, mapped, tmp_path)

        await self._load(session, _UNMAPPED_TTL, tmp_path)

        assert session.loaded_ontology == mapped

    async def test_a_mapped_ontology_is_activated_and_counted(self, tmp_path):
        session = self._session()
        ttl = _uploaded_ontology()

        result = await self._load(session, ttl, tmp_path)

        assert result["activated"] is True
        assert result["oba_requirements"]["mapped_tables"] == 2
        assert result["oba_requirements"]["mapped_joins"] == 1
        assert session.loaded_ontology == ttl
