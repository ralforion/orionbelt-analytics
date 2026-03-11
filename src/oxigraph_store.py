"""
Oxigraph RDF Store Manager

Provides persistent RDF storage with SPARQL 1.1 query support using Oxigraph.
Stores ontologies, schema metadata, and accumulated knowledge across sessions.
"""

import logging
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime

try:
    from pyoxigraph import Store, NamedNode, Literal, Triple, RdfFormat
    from pyoxigraph import parse as oxigraph_parse
    OXIGRAPH_AVAILABLE = True
except ImportError:
    OXIGRAPH_AVAILABLE = False
    Store = None
    NamedNode = None
    RdfFormat = None
    Literal = None
    Triple = None

logger = logging.getLogger(__name__)


class OxigraphStoreManager:
    """Manages persistent RDF storage using Oxigraph."""

    def __init__(self, store_path: Optional[Path] = None):
        """
        Initialize Oxigraph store manager.

        Args:
            store_path: Path to persistent store directory (None for in-memory)

        Raises:
            ImportError: If pyoxigraph is not installed
        """
        if not OXIGRAPH_AVAILABLE:
            raise ImportError(
                "pyoxigraph not installed. Install with: pip install pyoxigraph"
            )

        self.store_path = store_path

        if store_path:
            store_path.mkdir(parents=True, exist_ok=True)
            self.store = Store(str(store_path))
            logger.info(f"Initialized Oxigraph persistent store at: {store_path}")
        else:
            self.store = Store()
            logger.info("Initialized Oxigraph in-memory store")

        # Track loaded ontologies
        self._loaded_ontologies: Dict[str, str] = {}  # schema_name -> graph_uri

    def load_ontology(
        self,
        ontology_ttl: str,
        graph_uri: str,
        schema_name: str
    ) -> int:
        """
        Load ontology into the store.

        Args:
            ontology_ttl: Ontology in Turtle format
            graph_uri: Named graph URI for this ontology
            schema_name: Schema identifier

        Returns:
            Number of triples loaded
        """
        try:
            # Parse and load into named graph
            triples_before = len(self.store)

            # Use RdfFormat.TURTLE for newer versions, fall back to strings for older versions
            try:
                # Try with RdfFormat object (pyoxigraph >= 0.4.0)
                self.store.load(
                    ontology_ttl.encode('utf-8'),
                    format=RdfFormat.TURTLE,
                    base_iri=graph_uri,
                    to_graph=NamedNode(graph_uri)
                )
            except (TypeError, AttributeError):
                # Fallback for older pyoxigraph versions
                try:
                    self.store.load(
                        ontology_ttl.encode('utf-8'),
                        format="text/turtle",
                        base_iri=graph_uri,
                        to_graph=NamedNode(graph_uri)
                    )
                except TypeError:
                    # Final fallback for very old versions using mime_type
                    self.store.load(
                        ontology_ttl.encode('utf-8'),
                        mime_type="text/turtle",
                        base_iri=graph_uri,
                        to_graph=NamedNode(graph_uri)
                    )

            triples_after = len(self.store)
            triples_loaded = triples_after - triples_before

            self._loaded_ontologies[schema_name] = graph_uri

            logger.info(
                f"Loaded ontology for schema '{schema_name}' into graph <{graph_uri}>: "
                f"{triples_loaded} triples"
            )

            return triples_loaded

        except Exception as e:
            logger.error(f"Failed to load ontology: {e}", exc_info=True)
            raise

    def query_sparql(
        self,
        sparql_query: str,
        timeout_seconds: Optional[int] = 30
    ) -> List[Dict[str, Any]]:
        """
        Execute SPARQL query.

        Args:
            sparql_query: SPARQL query string
            timeout_seconds: Query timeout (None for no timeout)

        Returns:
            List of result bindings (each binding is a dict)

        Example:
            ```python
            results = store.query_sparql('''
                PREFIX db: <http://example.com/db#>
                SELECT ?table ?column
                WHERE {
                    ?table db:hasColumn ?column .
                    ?column db:dataType "INTEGER"
                }
                LIMIT 10
            ''')
            ```
        """
        try:
            results = []

            for solution in self.store.query(sparql_query):
                binding = {}
                for var, term in solution.items():
                    # Convert RDF terms to strings
                    if hasattr(term, 'value'):
                        binding[str(var)] = term.value
                    else:
                        binding[str(var)] = str(term)
                results.append(binding)

            logger.info(f"SPARQL query returned {len(results)} results")
            return results

        except Exception as e:
            logger.error(f"SPARQL query failed: {e}", exc_info=True)
            raise

    def query_sparql_ask(self, sparql_query: str) -> bool:
        """
        Execute SPARQL ASK query.

        Args:
            sparql_query: SPARQL ASK query

        Returns:
            Boolean result

        Example:
            ```python
            exists = store.query_sparql_ask('''
                PREFIX db: <http://example.com/db#>
                ASK {
                    ?table db:hasColumn ?column .
                    ?column db:dataType "INTEGER"
                }
            ''')
            ```
        """
        try:
            # For ASK queries, pyoxigraph returns a boolean directly
            # We need to cast the query result properly
            result = self.store.query(sparql_query)
            # ASK queries return a boolean, not an iterator
            # pyoxigraph query() returns the boolean value for ASK queries
            return bool(result) if isinstance(result, bool) else bool(next(iter(result), False))
        except StopIteration:
            return False
        except Exception as e:
            logger.error(f"SPARQL ASK query failed: {e}", exc_info=True)
            raise

    def query_sparql_construct(
        self,
        sparql_query: str
    ) -> str:
        """
        Execute SPARQL CONSTRUCT query.

        Args:
            sparql_query: SPARQL CONSTRUCT query

        Returns:
            Constructed RDF graph in Turtle format

        Example:
            ```python
            ttl = store.query_sparql_construct('''
                PREFIX db: <http://example.com/db#>
                CONSTRUCT {
                    ?table a db:IntegerTable
                }
                WHERE {
                    ?table db:hasColumn ?column .
                    ?column db:dataType "INTEGER"
                }
            ''')
            ```
        """
        try:
            # Execute query and serialize results
            results = self.store.query(sparql_query)
            # Oxigraph CONSTRUCT returns a graph
            return results.serialize(format="text/turtle")
        except Exception as e:
            logger.error(f"SPARQL CONSTRUCT query failed: {e}", exc_info=True)
            raise

    def add_triple(
        self,
        subject: str,
        predicate: str,
        object: str,
        graph_uri: Optional[str] = None,
        object_is_literal: bool = False
    ):
        """
        Add a single RDF triple to the store.

        Args:
            subject: Subject URI
            predicate: Predicate URI
            object: Object URI or literal value
            graph_uri: Optional named graph URI
            object_is_literal: If True, object is treated as literal value

        Example:
            ```python
            # Add a schema metadata triple
            store.add_triple(
                subject="http://example.com/schema/customers",
                predicate="http://www.w3.org/2000/01/rdf-schema#label",
                object="Customer Master Data",
                object_is_literal=True
            )
            ```
        """
        try:
            subj = NamedNode(subject)
            pred = NamedNode(predicate)
            obj = Literal(object) if object_is_literal else NamedNode(object)

            triple = Triple(subj, pred, obj)

            if graph_uri:
                self.store.add(triple, NamedNode(graph_uri))
            else:
                self.store.add(triple)

            logger.debug(f"Added triple: <{subject}> <{predicate}> {object}")

        except Exception as e:
            logger.error(f"Failed to add triple: {e}", exc_info=True)
            raise

    def add_knowledge(
        self,
        subject: str,
        predicate: str,
        object: str,
        metadata: Optional[Dict[str, Any]] = None,
        graph_uri: str = "http://example.com/knowledge"
    ):
        """
        Add learned knowledge to the store with metadata.

        Args:
            subject: Subject URI
            predicate: Predicate URI
            object: Object value
            metadata: Optional metadata (added as additional triples)
            graph_uri: Knowledge graph URI

        Example:
            ```python
            # Document a learned query pattern
            store.add_knowledge(
                subject="http://example.com/pattern/sales_by_customer",
                predicate="http://example.com/schema#hasSQL",
                object="SELECT customer_id, SUM(amount) FROM orders GROUP BY customer_id",
                metadata={
                    "learned_from": "user_query",
                    "timestamp": "2026-02-26T16:00:00Z",
                    "confidence": 0.95
                }
            )
            ```
        """
        try:
            # Add main triple
            self.add_triple(subject, predicate, object, graph_uri, object_is_literal=True)

            # Add metadata triples
            if metadata:
                for key, value in metadata.items():
                    meta_predicate = f"http://example.com/metadata#{key}"
                    self.add_triple(
                        subject,
                        meta_predicate,
                        str(value),
                        graph_uri,
                        object_is_literal=True
                    )

            logger.info(f"Added knowledge: {subject} -> {predicate}")

        except Exception as e:
            logger.error(f"Failed to add knowledge: {e}", exc_info=True)
            raise

    def get_ontology_stats(self, graph_uri: Optional[str] = None) -> Dict[str, Any]:
        """
        Get statistics about stored ontologies.

        Args:
            graph_uri: Optional specific graph to query

        Returns:
            Statistics dictionary
        """
        try:
            if graph_uri:
                # Count triples in specific graph
                query = f"""
                    SELECT (COUNT(*) AS ?count)
                    WHERE {{
                        GRAPH <{graph_uri}> {{
                            ?s ?p ?o .
                        }}
                    }}
                """
                results = list(self.store.query(query))
                triple_count = int(results[0]['count'].value) if results else 0

                return {
                    "graph_uri": graph_uri,
                    "triple_count": triple_count
                }
            else:
                # Overall statistics
                total_triples = len(self.store)

                # Count named graphs
                query = """
                    SELECT DISTINCT ?g
                    WHERE {
                        GRAPH ?g { ?s ?p ?o }
                    }
                """
                graphs = list(self.store.query(query))

                return {
                    "total_triples": total_triples,
                    "named_graphs": len(graphs),
                    "graphs": [str(g['g']) for g in graphs],
                    "loaded_ontologies": dict(self._loaded_ontologies)
                }

        except Exception as e:
            logger.error(f"Failed to get stats: {e}", exc_info=True)
            return {"error": str(e)}

    def list_tables_sparql(self, schema_graph: str) -> List[str]:
        """
        List all tables from an ontology using SPARQL.

        Args:
            schema_graph: Graph URI containing the ontology

        Returns:
            List of table names
        """
        query = f"""
            PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
            PREFIX db: <http://example.com/db#>

            SELECT DISTINCT ?tableName
            FROM <{schema_graph}>
            WHERE {{
                ?table a db:Table .
                ?table db:tableName ?tableName .
            }}
            ORDER BY ?tableName
        """

        results = self.query_sparql(query)
        return [r['tableName'] for r in results]

    def find_columns_by_type(
        self,
        data_type: str,
        schema_graph: Optional[str] = None
    ) -> List[Dict[str, str]]:
        """
        Find columns by data type using SPARQL.

        Args:
            data_type: SQL data type (e.g., "INTEGER", "VARCHAR")
            schema_graph: Optional graph to search

        Returns:
            List of {table, column, type} dicts
        """
        graph_clause = f"FROM <{schema_graph}>" if schema_graph else ""

        query = f"""
            PREFIX db: <http://example.com/db#>

            SELECT ?tableName ?columnName ?dataType
            {graph_clause}
            WHERE {{
                ?column a db:Column .
                ?column db:tableName ?tableName .
                ?column db:columnName ?columnName .
                ?column db:dataType ?dataType .
                FILTER (LCASE(STR(?dataType)) = LCASE("{data_type}"))
            }}
            ORDER BY ?tableName ?columnName
        """

        results = self.query_sparql(query)
        return [
            {
                "table": r['tableName'],
                "column": r['columnName'],
                "type": r['dataType']
            }
            for r in results
        ]

    def find_relationships(
        self,
        from_table: Optional[str] = None,
        to_table: Optional[str] = None,
        schema_graph: Optional[str] = None
    ) -> List[Dict[str, str]]:
        """
        Find foreign key relationships using SPARQL.

        Args:
            from_table: Optional source table filter
            to_table: Optional target table filter
            schema_graph: Optional graph to search

        Returns:
            List of relationship dicts
        """
        graph_clause = f"FROM <{schema_graph}>" if schema_graph else ""

        filters = []
        if from_table:
            filters.append(f'FILTER (?fromTable = "{from_table}")')
        if to_table:
            filters.append(f'FILTER (?toTable = "{to_table}")')

        filter_clause = "\n".join(filters)

        query = f"""
            PREFIX db: <http://example.com/db#>

            SELECT ?fromTable ?fromColumn ?toTable ?toColumn
            {graph_clause}
            WHERE {{
                ?rel a db:ForeignKey .
                ?rel db:fromTable ?fromTable .
                ?rel db:fromColumn ?fromColumn .
                ?rel db:toTable ?toTable .
                ?rel db:toColumn ?toColumn .
                {filter_clause}
            }}
            ORDER BY ?fromTable ?toTable
        """

        results = self.query_sparql(query)
        return [
            {
                "from_table": r['fromTable'],
                "from_column": r['fromColumn'],
                "to_table": r['toTable'],
                "to_column": r['toColumn']
            }
            for r in results
        ]

    def export_graph(
        self,
        graph_uri: str,
        format: str = "turtle"
    ) -> str:
        """
        Export a named graph.

        Args:
            graph_uri: Graph to export
            format: Export format ("turtle", "ntriples", "rdfxml")

        Returns:
            Serialized RDF
        """
        query = f"""
            CONSTRUCT {{ ?s ?p ?o }}
            WHERE {{
                GRAPH <{graph_uri}> {{
                    ?s ?p ?o
                }}
            }}
        """

        return self.query_sparql_construct(query)

    def clear_graph(self, graph_uri: str):
        """
        Clear all triples from a named graph.

        Args:
            graph_uri: Graph to clear
        """
        try:
            self.store.update(f"CLEAR GRAPH <{graph_uri}>")
            logger.info(f"Cleared graph: {graph_uri}")
        except Exception as e:
            logger.error(f"Failed to clear graph: {e}", exc_info=True)
            raise

    def backup(self, backup_path: Path):
        """
        Backup the entire store.

        Args:
            backup_path: Path to backup file (.nq or .trig format)
        """
        try:
            # Export all data
            all_data = self.store.dump_graph(format="application/n-quads")
            backup_path.write_bytes(all_data)
            logger.info(f"Backed up store to: {backup_path}")
        except Exception as e:
            logger.error(f"Backup failed: {e}", exc_info=True)
            raise

    def get_store_size(self) -> int:
        """Get total number of triples in store."""
        return len(self.store)

    def close(self):
        """Close the store (flush to disk if persistent)."""
        if hasattr(self.store, 'close'):
            self.store.close()
        logger.info("Closed Oxigraph store")
