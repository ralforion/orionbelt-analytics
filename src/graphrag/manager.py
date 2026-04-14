"""
GraphRAG Manager - Main orchestrator for GraphRAG operations

Coordinates embeddings, vector search, graph traversal, and community detection
to provide intelligent schema navigation and context-aware query generation.
"""

import logging
from typing import List, Dict, Any, Optional
from pathlib import Path
import json

from .embedder import SchemaEmbedder
from .retriever import GraphRetriever
from .community_detector import CommunityDetector

# Try to use ChromaDB if available, fallback to JSON-based VectorStore
try:
    from .vector_store_chromadb import ChromaDBVectorStore, CHROMADB_AVAILABLE
    if CHROMADB_AVAILABLE:
        logger = logging.getLogger(__name__)
        logger.info("ChromaDB available - using high-performance vector storage")
    else:
        logger = logging.getLogger(__name__)
        logger.warning("ChromaDB not available - falling back to JSON-based vector storage")
except ImportError:
    CHROMADB_AVAILABLE = False
    logger = logging.getLogger(__name__)
    logger.warning("ChromaDB not available - falling back to JSON-based vector storage")


class GraphRAGManager:
    """Main manager for GraphRAG operations."""

    def __init__(
        self,
        embedding_model: str = "tfidf",
        embedding_dimension: int = 384,
        connection_id: Optional[str] = None,
        schema_name: Optional[str] = None
    ):
        """
        Initialize GraphRAG manager.

        Args:
            embedding_model: Type of embedding ("tfidf", "sentence-transformers")
            embedding_dimension: Embedding vector dimension
            connection_id: Database connection fingerprint (for file isolation)
            schema_name: Schema name (for ChromaDB collection naming)
        """
        self.embedder = SchemaEmbedder(embedding_model=embedding_model)

        # Use ChromaDB if available, otherwise fallback to JSON-based storage
        if CHROMADB_AVAILABLE:
            self.vector_store = ChromaDBVectorStore(
                connection_id=connection_id or "default",
                schema_name=schema_name or "default",
                dimension=embedding_dimension
            )
            logger.info("Initialized ChromaDB vector store")
        else:
            from .vector_store import VectorStore
            self.vector_store = VectorStore(dimension=embedding_dimension)
            logger.warning("Using JSON-based vector store (ChromaDB not available)")

        self.graph_retriever = GraphRetriever()
        self.community_detector: Optional[CommunityDetector] = None

        self._initialized = False
        self._schema_name: Optional[str] = schema_name
        self._connection_id: Optional[str] = connection_id or "default"

    def initialize_from_schema(
        self,
        tables_info: List[Dict[str, Any]],
        schema_name: str = "default"
    ):
        """
        Initialize GraphRAG from schema metadata.

        Args:
            tables_info: List of table metadata dictionaries
            schema_name: Schema identifier
        """
        logger.info(f"Initializing GraphRAG for schema '{schema_name}' with {len(tables_info)} tables")

        self._schema_name = schema_name

        # Step 1: Create embeddings
        logger.info("Creating embeddings...")
        embeddings = self.embedder.batch_embed_schema(tables_info)

        # Step 2: Add to vector store
        logger.info("Building vector store...")
        self.vector_store.add_elements_batch(embeddings["tables"])
        self.vector_store.add_elements_batch(embeddings["columns"])
        self.vector_store.add_elements_batch(embeddings["relationships"])
        self.vector_store.build_index()

        # Step 3: Build graph
        logger.info("Building relationship graph...")
        self.graph_retriever.build_graph(tables_info)

        # Step 4: Detect communities
        logger.info("Detecting schema communities...")
        self.community_detector = CommunityDetector(self.graph_retriever.graph)
        self.community_detector.detect_communities(method="label_propagation")

        self._initialized = True
        logger.info("GraphRAG initialization complete")

    def search_schema(
        self,
        query: str,
        top_k: int = 5,
        element_type: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Search schema using natural language.

        Args:
            query: Natural language query
            top_k: Number of results
            element_type: Filter by type ("table", "column", "relationship")

        Returns:
            List of matching schema elements with scores
        """
        if not self._initialized:
            raise RuntimeError("GraphRAG not initialized. Call initialize_from_schema() first.")

        results = self.vector_store.search_by_text(
            query_text=query,
            embedder=self.embedder,
            top_k=top_k,
            element_type=element_type
        )

        return [
            {
                "element": {
                    "type": elem.element_type,
                    "id": elem.element_id,
                    "name": elem.name,
                    "description": elem.description,
                    "metadata": elem.metadata
                },
                "similarity_score": float(score)
            }
            for elem, score in results
        ]

    def find_relevant_tables(
        self,
        query: str,
        top_k: int = 5,
        include_related: bool = True,
        max_related_distance: int = 1
    ) -> Dict[str, Any]:
        """
        Find tables relevant to a natural language query.

        Args:
            query: Natural language description of what user wants
            top_k: Number of primary tables to find
            include_related: Whether to include related tables
            max_related_distance: Maximum graph distance for related tables

        Returns:
            Dictionary with primary tables, related tables, and context
        """
        if not self._initialized:
            raise RuntimeError("GraphRAG not initialized")

        # Step 1: Vector search for relevant tables
        table_results = self.search_schema(query, top_k=top_k, element_type="table")

        primary_tables = [r["element"]["name"] for r in table_results]

        result = {
            "primary_tables": table_results,
            "related_tables": {},
            "communities": {},
            "suggested_joins": []
        }

        if not primary_tables:
            return result

        # Step 2: Find related tables via graph traversal
        if include_related:
            all_related = {}
            for table in primary_tables:
                related = self.graph_retriever.get_related_tables(
                    table,
                    max_distance=max_related_distance
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
                        "tables_in_community": list(self.community_detector.get_community_tables(comm_id))
                    }

        # Step 4: Find join paths between primary tables
        if len(primary_tables) > 1:
            for i, table_a in enumerate(primary_tables[:-1]):
                for table_b in primary_tables[i+1:]:
                    join_path = self.graph_retriever.find_join_path(table_a, table_b)
                    if join_path:
                        result["suggested_joins"].append({
                            "from": table_a,
                            "to": table_b,
                            "path": join_path
                        })

        # Step 5: Check for fan-trap risks
        all_tables = list(set(primary_tables + [
            t for related in result["related_tables"].values()
            for tables in related.values()
            for t in tables
        ]))

        fan_trap_warnings = self.graph_retriever.detect_fan_traps(all_tables)
        if fan_trap_warnings:
            result["fan_trap_warnings"] = fan_trap_warnings

        return result

    def get_query_context(
        self,
        query: str,
        max_tables: int = 5,
        max_columns: int = 20
    ) -> Dict[str, Any]:
        """
        Get optimized context for SQL query generation.

        This is the main RAG retrieval function that returns minimal, relevant context.

        Args:
            query: Natural language query or SQL requirement
            max_tables: Maximum tables to include
            max_columns: Maximum columns to include

        Returns:
            Optimized context dictionary
        """
        if not self._initialized:
            raise RuntimeError("GraphRAG not initialized")

        # Find relevant tables
        table_info = self.find_relevant_tables(
            query,
            top_k=max_tables,
            include_related=True,
            max_related_distance=1
        )

        # Find relevant columns
        column_results = self.search_schema(
            query,
            top_k=max_columns,
            element_type="column"
        )

        # Build minimal context
        context = {
            "schema": self._schema_name,
            "relevant_tables": [],
            "relevant_columns": [],
            "relationships": table_info.get("suggested_joins", []),
            "fan_trap_warnings": table_info.get("fan_trap_warnings", []),
            "token_estimate": 0
        }

        # Add primary tables with their metadata
        for table_result in table_info["primary_tables"]:
            table_name = table_result["element"]["name"]
            table_meta = self.graph_retriever.get_table_metadata(table_name)

            if table_meta:
                context["relevant_tables"].append({
                    "name": table_name,
                    "relevance_score": table_result["similarity_score"],
                    "column_count": len(table_meta.get("columns", [])),
                    "has_foreign_keys": bool(table_meta.get("foreign_keys")),
                    "comment": table_meta.get("comment")
                })

        # Add relevant columns
        for col_result in column_results:
            context["relevant_columns"].append({
                "table": col_result["element"]["metadata"]["table"],
                "column": col_result["element"]["name"],
                "data_type": col_result["element"]["metadata"]["data_type"],
                "relevance_score": col_result["similarity_score"]
            })

        # Estimate token usage (rough approximation)
        context["token_estimate"] = (
            len(context["relevant_tables"]) * 200 +  # ~200 tokens per table summary
            len(context["relevant_columns"]) * 50 +   # ~50 tokens per column
            len(context["relationships"]) * 100       # ~100 tokens per join
        )

        return context

    def get_schema_overview(self) -> Dict[str, Any]:
        """
        Get high-level schema overview.

        Returns:
            Schema statistics and summaries
        """
        if not self._initialized:
            raise RuntimeError("GraphRAG not initialized")

        overview = {
            "schema_name": self._schema_name,
            "vector_store_stats": self.vector_store.get_statistics(),
            "graph_summary": self.graph_retriever.get_graph_summary()
        }

        if self.community_detector:
            overview["communities"] = self.community_detector.get_all_summaries()
            overview["domain_suggestions"] = self.community_detector.suggest_domain_names()

        return overview

    def save_state(self, output_dir: Path):
        """
        Save GraphRAG state to disk.

        Args:
            output_dir: Output directory
        """
        output_dir = Path(output_dir)

        # Create connection-specific subdirectory to prevent collisions
        connection_dir = output_dir / self._connection_id
        connection_dir.mkdir(parents=True, exist_ok=True)

        # Save vector store
        vector_store_path = connection_dir / f"vector_store_{self._schema_name}.json"
        self.vector_store.save(vector_store_path)

        # Save graph
        graph_path = connection_dir / f"graph_{self._schema_name}.json"
        graph_data = self.graph_retriever.export_graph_for_visualization()
        with open(graph_path, 'w') as f:
            json.dump(graph_data, f, indent=2)

        # Save communities
        if self.community_detector:
            communities_path = connection_dir / f"communities_{self._schema_name}.json"
            communities_data = {
                "summaries": self.community_detector.get_all_summaries(),
                "domain_names": self.community_detector.suggest_domain_names()
            }
            with open(communities_path, 'w') as f:
                json.dump(communities_data, f, indent=2)

        logger.info(f"Saved GraphRAG state to {connection_dir}")

    def clear(self):
        """Clear all GraphRAG state."""
        self.vector_store.clear()
        self.graph_retriever = GraphRetriever()
        self.community_detector = None
        self._initialized = False
        self._schema_name = None
        logger.info("Cleared GraphRAG state")
