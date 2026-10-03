"""
GraphRAG Manager - Main orchestrator for GraphRAG operations

Coordinates embeddings, vector search, graph traversal, and community detection
to provide intelligent schema navigation and context-aware query generation.
"""

import asyncio
import json
import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .community_detector import CommunityDetector
from .embedder import MODEL_TFIDF, SchemaEmbedder
from .identity import display_name, qualified
from .retriever import GraphRetriever

if TYPE_CHECKING:
    from .vector_store import VectorStore

# Try to use ChromaDB if available, fallback to JSON-based VectorStore
try:
    from .vector_store_chromadb import CHROMADB_AVAILABLE, ChromaDBVectorStore

    if CHROMADB_AVAILABLE:
        logger = logging.getLogger(__name__)
        logger.info("ChromaDB available - using high-performance vector storage")
    else:
        logger = logging.getLogger(__name__)
        logger.warning(
            "ChromaDB not available - falling back to JSON-based vector storage"
        )
except ImportError:
    CHROMADB_AVAILABLE = False
    logger = logging.getLogger(__name__)
    logger.warning("ChromaDB not available - falling back to JSON-based vector storage")


def _annotate_view_sources(
    views_info: list[dict[str, Any]] | None,
    dialect: str | None = None,
) -> list[dict[str, Any]]:
    """Fill in each view's ``referenced_tables`` by parsing its definition.

    A view body names the base tables it reads, so the sources can be
    recovered without asking the database a second question. Knowing them
    makes the view findable by the vocabulary of its inputs, not only its own
    name -- a search for "clients" should surface ``v_monthly_revenue`` when
    that view reads the clients table.

    Parse failures are not errors: view bodies come back in dialect-specific
    forms, and a view that cannot be parsed is still worth indexing by name
    and raw text. Such a view simply keeps an empty source list.

    The dialect matters more than the SQL does. Measured across real view
    bodies, sqlglot parses semi-structured Snowflake, BigQuery structs,
    ClickHouse aggregates and LATERAL joins without complaint -- and fails on
    a Snowflake body read as PostgreSQL. Passing the connection's dialect is
    what makes the difference between lineage and silence.

    Args:
        views_info: View metadata, or None.
        dialect: sqlglot dialect name. None uses sqlglot's permissive
            default, which is right only when the backend is unknown.

    Returns:
        The same list with ``referenced_tables`` populated where derivable.
        Returns an empty list when given None.
    """
    if not views_info:
        return []

    import sqlglot
    from sqlglot import exp

    for view in views_info:
        if view.get("referenced_tables"):
            continue

        definition = view.get("definition")
        if not definition:
            view["referenced_tables"] = []
            continue

        try:
            parsed = sqlglot.parse_one(definition, dialect=dialect)
            # CTE names are defined by the view itself, so they are not
            # sources; excluding them keeps the list to real base tables.
            excluded = {cte.alias_or_name.lower() for cte in parsed.find_all(exp.CTE)}
            # Nor is the view itself. DuckDB returns the whole CREATE VIEW
            # statement, whose target parses as a Table like any other, so the
            # view listed itself among its own sources.
            if isinstance(parsed, exp.Create):
                for target in parsed.this.find_all(exp.Table):
                    if target.name:
                        excluded.add(target.name.lower())
            excluded.add(str(view.get("name", "")).lower())

            sources = {
                table.name
                for table in parsed.find_all(exp.Table)
                if table.name and table.name.lower() not in excluded
            }
            view["referenced_tables"] = sorted(sources)
        except Exception as e:
            logger.debug(
                f"Could not parse definition of view '{view.get('name')}' "
                f"({e}); indexing it without source tables."
            )
            view["referenced_tables"] = []

    return views_info


class GraphRAGManager:
    """Main manager for GraphRAG operations."""

    def __init__(
        self,
        embedding_model: str | None = None,
        embedding_dimension: int = 384,
        connection_id: str | None = None,
        schema_name: str | None = None,
    ):
        """
        Initialize GraphRAG manager.

        Args:
            embedding_model: Backend name ("minilm", "tfidf",
                "sentence-transformers"), or None to resolve from
                GRAPHRAG_EMBEDDING_MODEL.
            embedding_dimension: Embedding vector dimension
            connection_id: Database connection fingerprint (for file isolation)
            schema_name: Schema name (for ChromaDB collection naming)
        """
        self.embedder = SchemaEmbedder(embedding_model=embedding_model)
        # Read back from the embedder rather than the argument: it may have
        # degraded to TF-IDF, and the store must record what actually produced
        # the vectors.
        resolved_model = self.embedder.embedding_model

        # Use ChromaDB if available, otherwise fallback to JSON-based storage.
        # Quoted deliberately: neither name is guaranteed bound at runtime --
        # ChromaDBVectorStore is unbound if the import above raised ImportError,
        # and VectorStore is TYPE_CHECKING-only plus locally imported below.
        self.vector_store: "ChromaDBVectorStore | VectorStore"  # noqa: UP037
        if CHROMADB_AVAILABLE:
            self.vector_store = ChromaDBVectorStore(
                connection_id=connection_id or "default",
                schema_name=schema_name or "default",
                dimension=embedding_dimension,
                embedding_model=resolved_model,
            )
            logger.info("Initialized ChromaDB vector store")
        else:
            from .vector_store import VectorStore

            self.vector_store = VectorStore(
                dimension=embedding_dimension, embedding_model=resolved_model
            )
            logger.warning("Using JSON-based vector store (ChromaDB not available)")

        self.graph_retriever = GraphRetriever()
        self.community_detector: CommunityDetector | None = None

        self._initialized = False
        self._schema_name: str | None = schema_name
        self._schema_names: list[str] = []
        self._connection_id: str = connection_id or "default"

    def initialize_from_schema(
        self,
        tables_info: list[dict[str, Any]],
        schema_name: str = "default",
        views_info: list[dict[str, Any]] | None = None,
    ) -> None:
        """
        Initialize GraphRAG from schema metadata.

        Args:
            tables_info: List of table metadata dictionaries
            schema_name: Schema identifier
            views_info: Optional view metadata. Views are indexed for search
                only -- they are not added to the relationship graph and never
                reach the ontology.
        """
        logger.info(
            f"Initializing GraphRAG for schema '{schema_name}' with "
            f"{len(tables_info)} tables and {len(views_info or [])} views"
        )

        self._schema_name = schema_name
        self._publish_schema(
            self._prepare_schema(tables_info, views_info),
            tables_info,
            schema_name,
            accumulate=False,
        )
        logger.info("GraphRAG initialization complete")

    def accumulate_schema(
        self,
        tables_info: list[dict[str, Any]],
        schema_name: str = "default",
        views_info: list[dict[str, Any]] | None = None,
    ) -> None:
        """Add a schema's tables to an already-initialized GraphRAG (accumulative).

        Unlike initialize_from_schema(), this does not clear existing data.
        New tables and embeddings are added alongside existing ones, enabling
        cross-schema join path discovery and unified semantic search.

        Args:
            tables_info: List of table metadata dictionaries
            schema_name: Schema identifier being added
            views_info: Optional view metadata, indexed for search only.
        """
        logger.info(
            f"Accumulating schema '{schema_name}' into GraphRAG "
            f"({len(tables_info)} tables, {len(views_info or [])} views, "
            f"existing schemas: {self._schema_names})"
        )

        self._publish_schema(
            self._prepare_schema(tables_info, views_info),
            tables_info,
            schema_name,
            accumulate=True,
        )

        logger.info(
            f"Schema '{schema_name}' accumulated. Total schemas: {self._schema_names}, "
            f"Total tables: {self.graph_retriever.graph.number_of_nodes()}"
        )

    def _try_embed(self, query: str) -> Any | None:
        """Embed a query, or return None so the search falls back to text.

        Args:
            query: The query text.

        Returns:
            The vector, or None if the backend could not embed it.
        """
        try:
            return self.embedder._embed_text(query)
        except Exception as e:
            logger.debug(f"Could not pre-embed the query, searching by text: {e}")
            return None

    async def aget_query_context(
        self,
        query: str,
        max_tables: int = 5,
        max_columns: int = 20,
        retriever: GraphRetriever | None = None,
    ) -> dict[str, Any]:
        """:meth:`get_query_context`, with the embedding off the event loop.

        Embedding the query is 75 ms of the ~82 a retrieval costs on MiniLM, and
        it ran on the loop for every call. It reads only the text and the
        embedder, so it moves to a worker. The searches stay here: they read
        the graph and the index, which indexing rewrites on the loop in one
        block, and on the loop they see it before that block or after it --
        from a thread they could see it half-way.

        Args:
            query: Natural language query or SQL requirement.
            max_tables: Maximum tables to include.
            max_columns: Maximum columns to include.
            retriever: The join graph to read instead of the shared one.

        Returns:
            What :meth:`get_query_context` returns.
        """
        embedding = await asyncio.to_thread(self._try_embed, query)
        return self.get_query_context(
            query,
            max_tables,
            max_columns,
            query_embedding=embedding,
            retriever=retriever,
        )

    async def asearch_schema(
        self, query: str, top_k: int = 5, element_type: str | None = None
    ) -> list[dict[str, Any]]:
        """:meth:`search_schema`, with the embedding off the event loop.

        Args:
            query: Natural language query.
            top_k: Number of results.
            element_type: Filter by type, or None for all.

        Returns:
            What :meth:`search_schema` returns.
        """
        embedding = await asyncio.to_thread(self._try_embed, query)
        return self.search_schema(
            query, top_k=top_k, element_type=element_type, query_embedding=embedding
        )

    def _prepare_schema(
        self,
        tables_info: list[dict[str, Any]],
        views_info: list[dict[str, Any]] | None,
    ) -> dict[str, list[Any]]:
        """Build the embeddings for a schema, touching no shared state.

        This is where indexing spends its time -- 8.2 s of the 8.3 s a
        60-table schema costs on MiniLM, the default backend -- and it reads
        only its arguments and the embedder, so it is the half that can run off
        the event loop.

        Args:
            tables_info: Table metadata to embed.
            views_info: View metadata to embed, or None.

        Returns:
            Elements by kind, as ``batch_embed_schema`` returns them.
        """
        logger.info("Creating embeddings...")
        return self.embedder.batch_embed_schema(
            tables_info, _annotate_view_sources(views_info)
        )

    def _publish_schema(
        self,
        embeddings: dict[str, list[Any]],
        tables_info: list[dict[str, Any]],
        schema_name: str,
        *,
        accumulate: bool,
    ) -> None:
        """Write prepared embeddings and the graph into the shared state.

        Synchronous on purpose, with no await inside: every reader of this
        manager runs on the same event loop, so an atomic block is what lets
        them see either the schema or not, never a graph mid-rebuild.

        Args:
            embeddings: What :meth:`_prepare_schema` produced.
            tables_info: The same table metadata, for the relationship graph.
            schema_name: The schema being indexed.
            accumulate: Add to the existing graph rather than replacing it.
        """
        # What this schema used to hold and no longer does. Rediscovery is how
        # a dropped table is noticed, and until now nothing acted on it: the
        # node kept offering join paths and the vectors kept turning up in
        # search, for a table SQL can no longer name.
        #
        # Only tables this schema owns are candidates. Nodes are keyed by bare
        # name, so two schemas holding a table of the same name share one node,
        # and a name last seen under another schema is left alone -- the same
        # rule the edge replacement follows, for the same reason.
        # Identities on both sides. Membership records qualified identities, so
        # comparing it with bare names made every table look dropped: each one
        # was removed and re-added, and the edges *other* schemas had into it
        # were not re-added with it.
        present = {
            qualified(table.get("schema"), table["name"]) for table in tables_info
        }
        dropped = self.graph_retriever.tables_of_schema(schema_name) - present
        dropped -= self.graph_retriever.tables_claimed_elsewhere(schema_name, dropped)
        if dropped:
            logger.info(
                f"Schema '{schema_name}' no longer reports {len(dropped)} "
                f"table(s): {sorted(dropped)}"
            )
            self.graph_retriever.remove_tables(dropped)
            try:
                self.vector_store.delete_tables(sorted(dropped))
            except Exception as e:
                # The graph is already consistent; a stale vector is a search
                # nuisance, not a wrong join, so do not fail the discovery.
                logger.warning(f"Could not delete vectors for dropped tables: {e}")

        logger.info("Building vector store...")
        self.vector_store.add_elements_batch(embeddings["tables"])
        self.vector_store.add_elements_batch(embeddings["columns"])
        self.vector_store.add_elements_batch(embeddings["relationships"])
        self.vector_store.add_elements_batch(embeddings["views"])
        self.vector_store.build_index()

        if accumulate:
            logger.info("Adding to relationship graph...")
            self.graph_retriever.add_to_graph(tables_info)
            logger.info("Re-detecting communities on combined graph...")
        else:
            logger.info("Building relationship graph...")
            self.graph_retriever.build_graph(tables_info)
            logger.info("Detecting schema communities...")

        self.community_detector = CommunityDetector(self.graph_retriever.graph)
        self.community_detector.detect_communities(method="label_propagation")

        self._schema_name = schema_name
        if schema_name not in self._schema_names:
            self._schema_names.append(schema_name)
        self._initialized = True

    async def aindex_schema(
        self,
        tables_info: list[dict[str, Any]],
        schema_name: str = "default",
        views_info: list[dict[str, Any]] | None = None,
        *,
        accumulate: bool,
    ) -> None:
        """Index a schema with the embedding work off the event loop.

        Embedding is 99% of indexing on the default backend and held the loop
        for its whole duration, so every other session on the server was frozen
        while one schema was indexed -- 8.2 s for 60 tables. It runs in a worker
        here, and only the writes to the shared graph and vector store happen
        back on the loop, in one synchronous block.

        The caller serializes calls per connection: the embedder fits its
        vocabulary as it goes, and two concurrent indexings of one manager would
        interleave their writes.

        Args:
            tables_info: Table metadata to index.
            schema_name: The schema being indexed.
            views_info: View metadata, indexed for search only.
            accumulate: Add to the existing graph rather than replacing it.
        """
        embeddings = await asyncio.to_thread(
            self._prepare_schema, tables_info, views_info
        )
        self._publish_schema(
            embeddings, tables_info, schema_name, accumulate=accumulate
        )
        logger.info(
            f"Schema '{schema_name}' indexed. Total schemas: {self._schema_names}, "
            f"Total tables: {self.graph_retriever.graph.number_of_nodes()}"
        )

    def search_schema(
        self,
        query: str,
        top_k: int = 5,
        element_type: str | None = None,
        query_embedding: Any | None = None,
    ) -> list[dict[str, Any]]:
        """
        Search schema using natural language.

        Args:
            query: Natural language query
            top_k: Number of results
            element_type: Filter by type ("table", "column", "relationship")
            query_embedding: The query already embedded. Pass it when the same
                query is searched more than once, so the text is put through
                the model only once; the vector depends on the text alone, not
                on which element type is being searched.

        Returns:
            List of matching schema elements with scores
        """
        if not self._initialized:
            raise RuntimeError(
                "GraphRAG not initialized. Call initialize_from_schema() first."
            )

        if query_embedding is None:
            results = self.vector_store.search_by_text(
                query_text=query,
                embedder=self.embedder,
                top_k=top_k,
                element_type=element_type,
            )
        else:
            results = self.vector_store.search(
                query_embedding, top_k=top_k, element_type=element_type
            )

        return [
            {
                "element": {
                    "type": elem.element_type,
                    "id": elem.element_id,
                    "name": elem.name,
                    "description": elem.description,
                    "metadata": elem.metadata,
                },
                "similarity_score": float(score),
            }
            for elem, score in results
        ]

    def add_semantic_context(
        self,
        target: str,
        context: str,
        source: str = "client",
    ) -> dict[str, Any]:
        """Index client-supplied business context for a table or column.

        Schema search can only match what the schema says about itself, which
        is usually abbreviations: ``salesamount``, ``unitcost``,
        ``returnquantity``. Nothing in those names carries the vocabulary users
        actually ask in -- "profit", "margin", "churn" -- so the concepts are
        unreachable no matter how good the embedding model is.

        This lets the calling model write that missing vocabulary into the
        index as an additional searchable element. It does not modify the
        schema element itself, so re-running discovery cannot silently
        overwrite it and the original description stays intact.

        The context is indexed only; it is not written to the ontology or RDF
        store, and it does not survive a backend switch or index rebuild (both
        discard derived vectors). Treat it as session enrichment, not durable
        knowledge.

        Calling this again for the same target replaces the previous context
        rather than adding a second entry, so it can be revised.

        Under the ``tfidf`` backend the context is stored but is effectively
        unsearchable: that vectorizer's vocabulary is fixed when the schema is
        indexed, so words the context introduces -- exactly the ones worth
        adding -- are out of vocabulary and score 0.0. The returned
        ``searchable`` flag reports this so callers are not misled.

        Args:
            target: Schema element the context describes, as ``table`` or
                ``table.column``. Recorded in metadata so results can be traced
                back; it does not have to exist yet.
            context: Business meaning in natural language. Include the words
                users would search with, and any formula worth surfacing.
            source: Where the context came from, for provenance in results.

        Returns:
            Summary of what was indexed: element id, target, character count,
            whether it replaced existing context, whether it is searchable, and
            a warning when it is not.

        Raises:
            RuntimeError: If GraphRAG has not been initialized.
            ValueError: If *target* or *context* is blank.
        """
        if not self._initialized:
            raise RuntimeError(
                "GraphRAG not initialized. Call initialize_from_schema() first."
            )

        target = target.strip()
        context = context.strip()
        if not target:
            raise ValueError("target must name a table or table.column")
        if not context:
            raise ValueError("context must not be empty")

        # The target is embedded alongside the prose so a search for the column
        # name still reaches its context, not only a search for the concept.
        description = f"{target.replace('.', ' ').replace('_', ' ')} {context}"
        embedding = self.embedder._embed_text(description)

        element_id = f"semantic_context:{target}"
        replaced = self.vector_store.get_by_id(element_id) is not None

        # upsert, not add: both stores keep the *first* write for an id -- the
        # ChromaDB collection ignores the second add and the JSON store appends
        # a duplicate whose lookups still return the stale entry -- so a revised
        # context would report success while the old one kept answering.
        self.vector_store.upsert_element(
            element_type="semantic_context",
            element_id=element_id,
            name=target,
            description=description,
            embedding=embedding,
            metadata={
                "target": target,
                "context": context,
                "source": source,
            },
        )
        self.vector_store.build_index()

        # TF-IDF fits its vocabulary on the schema corpus and never refits, so
        # the vocabulary this context introduces cannot be matched. Storing it
        # is harmless and keeps the record, but saying nothing would leave the
        # caller believing the concept is now findable.
        searchable = self.embedder.embedding_model != MODEL_TFIDF
        result: dict[str, Any] = {
            "element_id": element_id,
            "target": target,
            "characters": len(context),
            "replaced_existing": replaced,
            "searchable": searchable,
        }
        if not searchable:
            warning = (
                "Stored but not searchable: the 'tfidf' backend fixes its "
                "vocabulary when the schema is indexed, so words this context "
                "introduces score 0.0. Set GRAPHRAG_EMBEDDING_MODEL=minilm and "
                "re-run discover_schema() to make enrichment effective."
            )
            result["warning"] = warning
            logger.warning(f"Semantic context for '{target}': {warning}")
        else:
            logger.info(
                f"Indexed semantic context for '{target}' ({len(context)} chars)"
            )

        return result

    def business_name_matches(
        self,
        query: str,
        top_k: int,
        retriever: GraphRetriever | None = None,
        query_embedding: Any | None = None,
    ) -> list[dict[str, Any]]:
        """Schema elements whose business name or description fits the query.

        Business names -- from ``apply_semantic_names`` or
        ``graphrag_add_semantic_context`` -- are indexed as entries of their
        own, keyed by the table or ``table.column`` they describe. Searching
        tables and columns alone never reads them, so a concept that exists
        only in the business vocabulary was unreachable from a question.

        Args:
            query: The question.
            top_k: How many business-name entries to consider.
            retriever: The join graph to resolve targets in.
            query_embedding: The question, already embedded.

        Returns:
            One match per entry that resolves to exactly one table: its
            ``table`` identity, ``column`` (or None), ``score`` and the
            ``business_name`` text. Relationship entries and names that fit
            two schemas are left out rather than guessed.
        """
        # TF-IDF fixed its vocabulary when the schema was indexed, so the
        # words a business name introduces score nothing (add_semantic_context
        # says so when it stores one).
        if self.embedder.embedding_model == MODEL_TFIDF:
            return []
        graph = retriever or self.graph_retriever
        try:
            hits = self.search_schema(
                query,
                top_k=top_k,
                element_type="semantic_context",
                query_embedding=query_embedding,
            )
        except Exception as e:
            logger.debug(f"Business-name search skipped: {e}")
            return []

        matches: list[dict[str, Any]] = []
        for hit in hits:
            meta = hit["element"].get("metadata") or {}
            target = str(meta.get("target") or "").strip()
            if not target or "__to__" in target:
                continue
            resolved = _resolve_target(target, graph)
            if resolved is None:
                continue
            table, column = resolved
            matches.append(
                {
                    "table": table,
                    "column": column,
                    "score": float(hit["similarity_score"]),
                    "business_name": str(meta.get("context") or ""),
                }
            )
        return matches

    def find_relevant_tables(
        self,
        query: str,
        top_k: int = 5,
        include_related: bool = True,
        max_related_distance: int = 1,
        query_embedding: Any | None = None,
        retriever: GraphRetriever | None = None,
        business_candidates: int | None = None,
    ) -> dict[str, Any]:
        """
        Find tables relevant to a natural language query.

        Args:
            query: Natural language description of what user wants
            query_embedding: The query already embedded, when the caller is
                searching the same text more than once.
            top_k: Number of primary tables to find
            include_related: Whether to include related tables
            max_related_distance: Maximum graph distance for related tables
            retriever: The join graph to read instead of the shared one -- a
                session's copy extended with its own ontology's relationships.
            business_candidates: How many business-name entries to consider;
                ``top_k`` if not given. The matches are also returned for the
                caller's column ranking, which may want more than ``top_k``.

        Returns:
            Dictionary with primary tables, related tables, and context
        """
        if not self._initialized:
            raise RuntimeError("GraphRAG not initialized")
        graph = retriever or self.graph_retriever

        # Step 1: Vector search for relevant tables
        table_results = self.search_schema(
            query, top_k=top_k, element_type="table", query_embedding=query_embedding
        )

        # Business names compete with the schema's own names. A table whose
        # columns are cryptic is found through the name someone gave it, or
        # gave one of its columns -- "revenue" reaching orders through
        # net_amt's "Net revenue".
        business = self.business_name_matches(
            query,
            business_candidates or top_k,
            graph,
            query_embedding=query_embedding,
        )
        table_results = _merge_table_hits(table_results, business, top_k)

        # The element id, not the display name: it carries the schema, so a
        # table of the same name in another schema is not confused with this
        # one when its joins and community are looked up.
        primary_tables = [r["element"]["id"] for r in table_results]

        result: dict[str, Any] = {
            "primary_tables": table_results,
            "related_tables": {},
            "communities": {},
            "suggested_joins": [],
            "business_name_matches": business,
        }

        if not primary_tables:
            return result

        # Step 2: Find related tables via graph traversal
        if include_related:
            all_related = {}
            for table in primary_tables:
                related = graph.get_related_tables(
                    table, max_distance=max_related_distance
                )
                all_related[table] = related

            result["related_tables"] = all_related

        # Step 3: Get community information
        if self.community_detector:
            for table in primary_tables:
                comm_id = self.community_detector.get_community(table)
                if comm_id is not None:
                    result["communities"][table] = {
                        "community_id": comm_id,
                        "tables_in_community": list(
                            self.community_detector.get_community_tables(comm_id)
                        ),
                    }

        # Step 4: Find join paths between primary tables
        if len(primary_tables) > 1:
            for i, table_a in enumerate(primary_tables[:-1]):
                for table_b in primary_tables[i + 1 :]:
                    join_path = graph.find_join_path(table_a, table_b)
                    if join_path:
                        result["suggested_joins"].append(
                            {"from": table_a, "to": table_b, "path": join_path}
                        )

        # Step 5: Check for fan-trap risks
        all_tables = list(
            set(
                primary_tables
                + [
                    t
                    for related in result["related_tables"].values()
                    for tables in related.values()
                    for t in tables
                ]
            )
        )

        fan_trap_warnings = graph.detect_fan_traps(all_tables)
        if fan_trap_warnings:
            result["fan_trap_warnings"] = fan_trap_warnings

        return result

    def get_query_context(
        self,
        query: str,
        max_tables: int = 5,
        max_columns: int = 20,
        query_embedding: Any | None = None,
        retriever: GraphRetriever | None = None,
    ) -> dict[str, Any]:
        """
        Get optimized context for SQL query generation.

        This is the main RAG retrieval function that returns minimal, relevant context.

        Args:
            query: Natural language query or SQL requirement
            max_tables: Maximum tables to include
            max_columns: Maximum columns to include
            query_embedding: The query already embedded, e.g. by
                :meth:`aget_query_context` in a worker. Embedded here if absent.
            retriever: The join graph to read instead of the shared one.

        Returns:
            Optimized context dictionary
        """
        if not self._initialized:
            raise RuntimeError("GraphRAG not initialized")

        # One embedding for both searches. The vector depends on the query text
        # alone, and this method searches tables and then columns with the same
        # text, so embedding it twice put the identical string through the model
        # for nothing.
        if query_embedding is None:
            query_embedding = self._try_embed(query)

        # Find relevant tables
        table_info = self.find_relevant_tables(
            query,
            top_k=max_tables,
            include_related=True,
            max_related_distance=1,
            query_embedding=query_embedding,
            retriever=retriever,
            # The matches also rank columns, so as many as either list keeps:
            # with max_tables below max_columns, column names were cut off.
            business_candidates=max(max_tables, max_columns),
        )

        # Find relevant columns
        column_results = self.search_schema(
            query,
            top_k=max_columns,
            element_type="column",
            query_embedding=query_embedding,
        )

        # Build minimal context
        context = {
            "schema": self._schema_name,
            "relevant_tables": [],
            "relevant_columns": [],
            "relationships": table_info.get("suggested_joins", []),
            "fan_trap_warnings": table_info.get("fan_trap_warnings", []),
            "token_estimate": 0,
        }

        # Add primary tables with their metadata
        for table_result in table_info["primary_tables"]:
            identity = table_result["element"]["id"]
            table_name = table_result["element"]["name"]
            table_meta = self.graph_retriever.get_table_metadata(identity)

            if table_meta:
                entry: dict[str, Any] = {
                    "name": table_name,
                    "relevance_score": table_result["similarity_score"],
                    "column_count": len(table_meta.get("columns", [])),
                    "has_foreign_keys": bool(table_meta.get("foreign_keys")),
                    "comment": table_meta.get("comment"),
                }
                if table_result.get("matched_business_name"):
                    entry["matched_business_name"] = table_result[
                        "matched_business_name"
                    ]
                context["relevant_tables"].append(entry)

        # Add relevant columns
        for col_result in column_results:
            context["relevant_columns"].append(
                {
                    "table": col_result["element"]["metadata"]["table"],
                    "column": col_result["element"]["name"],
                    "data_type": col_result["element"]["metadata"]["data_type"],
                    "relevance_score": col_result["similarity_score"],
                }
            )
        context["relevant_columns"] = _merge_column_hits(
            context["relevant_columns"],
            table_info.get("business_name_matches", []),
            retriever or self.graph_retriever,
            max_columns,
        )

        # Estimate token usage (rough approximation)
        context["token_estimate"] = (
            len(context["relevant_tables"]) * 200
            + len(context["relevant_columns"]) * 50  # ~200 tokens per table summary
            + len(context["relationships"])  # ~50 tokens per column
            * 100  # ~100 tokens per join
        )

        return context

    def get_schema_overview(self) -> dict[str, Any]:
        """
        Get high-level schema overview.

        Returns:
            Schema statistics and summaries
        """
        if not self._initialized:
            raise RuntimeError("GraphRAG not initialized")

        overview: dict[str, Any] = {
            "schema_name": self._schema_name,
            "vector_store_stats": self.vector_store.get_statistics(),
            "graph_summary": self.graph_retriever.get_graph_summary(),
        }

        if self.community_detector:
            overview["communities"] = self.community_detector.get_all_summaries()
            overview["domain_suggestions"] = (
                self.community_detector.suggest_domain_names()
            )

        return overview

    @property
    def vector_collection_name(self) -> str:
        """Name of the backing ChromaDB collection, or "" when not using one.

        Recorded on each version so retention can tell whether a collection is
        still referenced by a live version before deleting it. Empty for the
        JSON fallback store, which has no collections.
        """
        collection = getattr(self.vector_store, "collection", None)
        return str(getattr(collection, "name", "") or "")

    @property
    def vector_count(self) -> int:
        """Number of elements currently in the vector store, 0 if unavailable."""
        try:
            stats = self.vector_store.get_statistics()
        except Exception as e:  # pragma: no cover - defensive
            logger.warning(f"Failed to read vector store statistics: {e}")
            return 0
        return int(stats.get("total_elements", 0) or 0)

    def save_state(
        self,
        output_dir: Path,
        version: int | None = None,
        snapshot_schema: str | None = None,
    ) -> list[str]:
        """
        Save GraphRAG state to disk.

        Saves combined state (all accumulated schemas) plus per-schema files
        for backward compatibility with workspace metadata.

        When *version* and *snapshot_schema* are both given, that one schema's
        files are also written under a ``_v{version}`` name. Those copies are
        the per-version history that retention prunes; the unversioned files
        stay the current-generation pointer that :meth:`load_state` reads, so
        they are never deletion candidates and restore cannot be broken by
        cleanup.

        Snapshotting is deliberately restricted to a single schema. Version
        numbers are per schema, and this manager holds every accumulated schema
        on the connection -- so applying one schema's version number across all
        of them would overwrite ``vector_store_public_v1.json`` when *analytics*
        v1 is saved, and hand ownership of public's snapshot to analytics's
        version record. Cleanup would then delete another schema's history.

        Args:
            output_dir: Output directory
            version: Version number to snapshot under, or None for no snapshot.
            snapshot_schema: The one schema to snapshot; required for a snapshot
                to be taken, and must be the schema *version* belongs to.

        Returns:
            Names of the versioned snapshot files written, relative to the
            connection directory. Empty when no snapshot was taken.
        """
        output_dir = Path(output_dir)
        snapshot_files: list[str] = []

        # Create connection-specific subdirectory to prevent collisions
        connection_dir = output_dir / self._connection_id
        connection_dir.mkdir(parents=True, exist_ok=True)

        # Combined graph with all schemas' tables_info
        all_tables_info = list(self.graph_retriever._tables_info.values())

        # Derived once and written into several files. The graph and the
        # communities are connection-wide, so recomputing them per accumulated
        # schema produced the same answer each time -- for five schemas, six
        # whole-graph exports and six rounds of community summarization.
        visualization = self.graph_retriever.export_graph_for_visualization()
        communities_data: dict[str, Any] | None = None
        if self.community_detector:
            communities_data = {
                "summaries": self.community_detector.get_all_summaries(),
                "domain_names": self.community_detector.suggest_domain_names(),
            }

        # The vocabulary the stored vectors were made against. Without it a
        # restart embeds a query against a vocabulary fitted on that query's
        # own words -- a different space of a different size, padded to the
        # same width by the store, so the search returns plausible nonsense
        # instead of failing.
        vocabulary = self.embedder.vocabulary_state()
        if vocabulary is not None:
            with open(connection_dir / "embedder_vocabulary.json", "w") as f:
                json.dump(vocabulary, f)

        # Save combined graph
        graph_path = connection_dir / "graph_combined.json"
        graph_data = {
            "schema_names": self._schema_names,
            "tables_info": all_tables_info,
            "visualization": visualization,
        }
        with open(graph_path, "w") as f:
            json.dump(graph_data, f, indent=2)

        # Save combined communities
        if communities_data is not None:
            communities_path = connection_dir / "communities_combined.json"
            with open(communities_path, "w") as f:
                json.dump(communities_data, f, indent=2)

        # Also save per-schema files (backward compat with workspace metadata)
        exported_vectors: Path | None = None
        for schema_name in self._schema_names:
            vector_store_path = connection_dir / f"vector_store_{schema_name}.json"
            # The vector store is connection-scoped and accumulative, so every
            # schema's file held an export of the same whole collection. It was
            # serialized once per schema: five schemas wrote 38 MB of five
            # identical exports, 915 ms against 206 ms. Exported once now, and
            # copied --
            # which also makes the files byte-identical, where before they
            # differed in the order ChromaDB happened to return metadata keys.
            if exported_vectors is None:
                self.vector_store.save(vector_store_path)
                exported_vectors = vector_store_path
            else:
                try:
                    shutil.copy2(exported_vectors, vector_store_path)
                except OSError as e:
                    logger.warning(
                        f"Failed to copy the vector export to "
                        f"{vector_store_path.name} ({e}); exporting again"
                    )
                    self.vector_store.save(vector_store_path)

            # Per-schema graph subset
            schema_tables = [
                t for t in all_tables_info if t.get("schema") == schema_name
            ]
            if not schema_tables:
                schema_tables = all_tables_info  # Fallback for single schema
            per_schema_graph_path = connection_dir / f"graph_{schema_name}.json"
            per_schema_data = {
                "tables_info": schema_tables,
                "visualization": visualization,
            }
            with open(per_schema_graph_path, "w") as f:
                json.dump(per_schema_data, f, indent=2)

            schema_communities_path: Path | None = None
            if communities_data is not None:
                schema_communities_path = (
                    connection_dir / f"communities_{schema_name}.json"
                )
                with open(schema_communities_path, "w") as f:
                    json.dump(communities_data, f, indent=2)

            # Snapshot only the schema whose version number this is.
            if version is None or schema_name != snapshot_schema:
                continue

            # Copied from the files just written rather than re-serialized, so
            # the snapshot is byte-identical to the state restore would load.
            for current in (
                vector_store_path,
                per_schema_graph_path,
                schema_communities_path,
            ):
                if current is None or not current.exists():
                    continue
                snapshot = current.with_name(
                    f"{current.stem}_v{version}{current.suffix}"
                )
                try:
                    shutil.copy2(current, snapshot)
                except OSError as e:
                    # A missing snapshot costs history for this generation, not
                    # correctness of the live state -- do not fail the save.
                    logger.warning(f"Failed to snapshot {current.name}: {e}")
                    continue
                snapshot_files.append(snapshot.name)

        logger.info(
            f"Saved GraphRAG state to {connection_dir} "
            f"(schemas: {self._schema_names})"
        )
        return snapshot_files

    def load_state(self, output_dir: Path) -> bool:
        """Restore GraphRAG state from disk.

        Prefers combined state (graph_combined.json) for multi-schema support.
        Falls back to per-schema files (graph_{schema}.json) for backward compat.
        ChromaDB vector store reconnects implicitly via get_or_create_collection.

        Args:
            output_dir: Base output directory (same as passed to save_state)

        Returns:
            True if state was fully restored, False if any component failed
        """
        output_dir = Path(output_dir)
        connection_dir = output_dir / self._connection_id

        if not connection_dir.exists():
            logger.warning(f"Connection dir not found: {connection_dir}")
            return False

        schema_name = self._schema_name or "default"
        restored_components = []

        # Before anything reads the vectors: they only mean something against
        # the vocabulary they were made with.
        vocabulary_path = connection_dir / "embedder_vocabulary.json"
        if vocabulary_path.exists():
            try:
                with open(vocabulary_path) as f:
                    saved_vocabulary = json.load(f)
                if self.embedder.load_vocabulary_state(saved_vocabulary):
                    restored_components.append("embedding vocabulary")
            except (OSError, ValueError) as e:
                logger.warning(f"Could not read the saved vocabulary: {e}")
        elif self.embedder.embedding_model == MODEL_TFIDF:
            logger.warning(
                "No saved TF-IDF vocabulary beside this index: it was written "
                "by an older version. Searches cannot be compared with the "
                "stored vectors until the schema is discovered again."
            )

        # 1. Verify ChromaDB has data (reconnected implicitly in __init__)
        try:
            stats = self.vector_store.get_statistics()
            vector_count = stats.get("total_elements", 0)
            if vector_count > 0:
                restored_components.append(f"vectors ({vector_count})")
            else:
                logger.warning(
                    "ChromaDB collection is empty — vector search will not work"
                )
        except Exception as e:
            logger.warning(f"Failed to verify ChromaDB: {e}")

        # 2. Load graph — prefer combined, fallback to per-schema
        combined_graph_path = connection_dir / "graph_combined.json"
        per_schema_graph_path = connection_dir / f"graph_{schema_name}.json"

        graph_path = (
            combined_graph_path
            if combined_graph_path.exists()
            else per_schema_graph_path
        )

        if graph_path.exists():
            try:
                with open(graph_path) as f:
                    graph_data = json.load(f)

                tables_info = graph_data.get("tables_info")
                if tables_info:
                    self.graph_retriever.load_graph(tables_info)
                    restored_components.append(
                        f"graph ({self.graph_retriever.graph.number_of_nodes()} tables)"
                    )
                    # Restore schema names list
                    saved_schemas = graph_data.get("schema_names")
                    if saved_schemas:
                        self._schema_names = saved_schemas
                    elif schema_name not in self._schema_names:
                        self._schema_names.append(schema_name)
                else:
                    logger.warning(
                        "Graph file missing tables_info — graph not restored"
                    )
            except Exception as e:
                logger.error(f"Failed to load graph: {e}")
        else:
            logger.warning(f"Graph file not found: {graph_path}")

        # 3. Load communities — prefer combined, fallback to per-schema
        combined_communities_path = connection_dir / "communities_combined.json"
        per_schema_communities_path = connection_dir / f"communities_{schema_name}.json"
        communities_path = (
            combined_communities_path
            if combined_communities_path.exists()
            else per_schema_communities_path
        )

        if communities_path.exists():
            try:
                with open(communities_path) as f:
                    communities_data = json.load(f)

                self.community_detector = CommunityDetector(self.graph_retriever.graph)
                if self.community_detector.load_communities(communities_data):
                    restored_components.append(
                        f"communities ({len(self.community_detector.communities)})"
                    )
                else:
                    logger.warning("Communities file empty — communities not restored")
            except Exception as e:
                logger.error(f"Failed to load communities: {e}")
        else:
            logger.debug(f"Communities file not found: {communities_path}")

        # Mark as initialized if we restored at least the graph
        if self.graph_retriever.graph.number_of_nodes() > 0:
            self._initialized = True
            logger.info(
                f"Restored GraphRAG state: {', '.join(restored_components)} "
                f"(schemas: {self._schema_names})"
            )
            return True

        logger.warning("GraphRAG restore failed — no graph data loaded")
        return False

    def clear(self) -> None:
        """Clear all GraphRAG state."""
        self.vector_store.clear()
        self.graph_retriever = GraphRetriever()
        self.community_detector = None
        self._initialized = False
        self._schema_name = None
        logger.info("Cleared GraphRAG state")


def _resolve_target(
    target: str, graph: GraphRetriever
) -> tuple[str, str | None] | None:
    """The table, and column if any, a business-name entry describes.

    Targets are ``table`` or ``table.column`` with bare names, as
    ``apply_semantic_names`` writes them -- joined with a dot although either
    name may contain one (a column ``net.amount`` makes ``orders.net.amount``).
    So no dot is assumed to be the separator: the whole target is tried as a
    table, then every dot as the split, and a split counts only where the
    table resolves and has that column.

    Args:
        target: The entry's target.
        graph: The join graph whose tables it is resolved against.

    Returns:
        ``(table identity, column or None)``, or None if nothing fits, the
        table is in two schemas, or two different readings fit.
    """
    readings: set[tuple[str, str | None]] = set()
    whole = graph.resolve_name(target)
    if whole.identity is not None and not whole.ambiguous:
        readings.add((whole.identity, None))
    for at, char in enumerate(target):
        if char != ".":
            continue
        table_name, column = target[:at], target[at + 1 :]
        if not table_name or not column:
            continue
        found = graph.resolve_name(table_name)
        if found.identity is None or found.ambiguous:
            continue
        meta = graph.get_table_metadata(found.identity) or {}
        if any(col.get("name") == column for col in meta.get("columns", [])):
            readings.add((found.identity, column))
    if len(readings) != 1:
        return None
    return readings.pop()


def _merge_table_hits(
    table_results: list[dict[str, Any]],
    business: list[dict[str, Any]],
    top_k: int,
) -> list[dict[str, Any]]:
    """Table search results with the tables business names point at, re-ranked.

    A table keeps the better of its own score and its best business-name
    score, and says which business name lifted it.

    Args:
        table_results: Results of the table search.
        business: :meth:`GraphRAGManager.business_name_matches` output.
        top_k: How many tables to keep.

    Returns:
        At most ``top_k`` table results, best first.
    """
    ranked: dict[str, dict[str, Any]] = {r["element"]["id"]: r for r in table_results}
    for match in business:
        identity = match["table"]
        held = ranked.get(identity)
        if held is not None and held["similarity_score"] >= match["score"]:
            continue
        element = (held or {}).get("element") or {
            "type": "table",
            "id": identity,
            "name": display_name(identity),
            "description": "",
            "metadata": {"table": identity},
        }
        ranked[identity] = {
            "element": element,
            "similarity_score": match["score"],
            "matched_business_name": match["business_name"],
        }
    return sorted(ranked.values(), key=lambda r: -r["similarity_score"])[:top_k]


def _merge_column_hits(
    columns: list[dict[str, Any]],
    business: list[dict[str, Any]],
    graph: GraphRetriever,
    max_columns: int,
) -> list[dict[str, Any]]:
    """Relevant columns with the columns business names point at, re-ranked.

    Args:
        columns: Columns found by the column search.
        business: :meth:`GraphRAGManager.business_name_matches` output.
        graph: The join graph, for each column's data type.
        max_columns: How many columns to keep.

    Returns:
        At most ``max_columns`` columns, best first; a column found through
        its business name carries it as ``business_name``.
    """
    by_key: dict[tuple[str, str], dict[str, Any]] = {
        (c["table"], c["column"]): c for c in columns
    }
    for match in business:
        if match["column"] is None:
            continue
        key = (match["table"], match["column"])
        held = by_key.get(key)
        if held is not None and held["relevance_score"] >= match["score"]:
            held.setdefault("business_name", match["business_name"])
            continue
        meta = graph.get_table_metadata(match["table"]) or {}
        data_type = next(
            (
                c.get("data_type")
                for c in meta.get("columns", [])
                if c.get("name") == match["column"]
            ),
            None,
        )
        by_key[key] = {
            "table": match["table"],
            "column": match["column"],
            "data_type": data_type,
            "relevance_score": match["score"],
            "business_name": match["business_name"],
        }
    return sorted(by_key.values(), key=lambda c: -c["relevance_score"])[:max_columns]
