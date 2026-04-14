"""Workspace restore handler implementation."""

import json
import logging
from typing import Optional

from fastmcp import Context

from ..database_manager import TableInfo
from ..graphrag import GraphRAGManager
from ..lifecycle.metadata import VersionMetadataManager
from ..oxigraph_store import OXIGRAPH_AVAILABLE
from ..paths import OUTPUT_DIR, get_connection_dir, ensure_output_dir

logger = logging.getLogger(__name__)


async def restore_workspace(
    ctx: Context,
    schema_name: Optional[str],
    get_session_data,
    get_oxigraph_store,
    create_error_response,
) -> str:
    """Restore workspace from previous session artifacts.

    Reloads schema cache, ontology, GraphRAG, and RDF store from disk
    so the user can continue where they left off without re-analyzing.

    Args:
        ctx: FastMCP context
        schema_name: Schema to restore (uses first available if not specified)
        get_session_data: Function to get session data
        get_oxigraph_store: Function to get/init Oxigraph store
        create_error_response: Function to create error response

    Returns:
        Restore status summary
    """
    session = get_session_data(ctx)

    if not session.connection_id:
        return create_error_response(
            "No database connection. Call connect_database first.",
            "connection_error",
        )

    connection_id = session.connection_id
    conn_dir = get_connection_dir(connection_id)

    # Load workspace metadata
    mgr = VersionMetadataManager(connection_id, OUTPUT_DIR)
    workspace = mgr.get_workspace()

    if not workspace:
        return create_error_response(
            "No workspace found for this connection. Use analyze_schema to start fresh.",
            "workspace_not_found",
        )

    schemas = workspace.get("schemas", {})
    if not schemas:
        return create_error_response(
            "Workspace has no schema data. Use analyze_schema to start fresh.",
            "workspace_empty",
        )

    # Resolve schema name
    if not schema_name:
        schema_name = list(schemas.keys())[0]
        if len(schemas) > 1:
            await ctx.info(
                f"Multiple schemas available: {', '.join(schemas.keys())}. "
                f"Restoring '{schema_name}'. Call again with schema_name to restore a different one."
            )

    schema_data = schemas.get(schema_name)
    if not schema_data:
        available = ", ".join(schemas.keys())
        return create_error_response(
            f"Schema '{schema_name}' not found in workspace. Available: {available}",
            "schema_not_found",
        )

    restored = []
    failed = []

    # 1. Restore schema cache
    schema_section = schema_data.get("schema", {})
    schema_file = schema_section.get("schema_file")
    if schema_file:
        schema_path = conn_dir / schema_file
        if schema_path.exists():
            try:
                with open(schema_path, "r", encoding="utf-8") as f:
                    schema_json = json.load(f)

                # Deserialize to List[TableInfo]
                tables_raw = schema_json.get("tables", [])
                tables_info = [TableInfo.from_dict(t) for t in tables_raw]

                session.cache_schema_analysis(schema_name, tables_info)
                session.schema_file = schema_file
                restored.append(
                    f"Schema analysis: {len(tables_info)} tables"
                )
            except Exception as e:
                logger.error(f"Failed to restore schema cache: {e}")
                failed.append(f"Schema cache: {e}")
        else:
            failed.append(f"Schema file missing: {schema_file}")

    # 2. Restore ontology
    ontology_section = schema_data.get("ontology", {})
    ontology_file = ontology_section.get("ontology_file")
    if ontology_file:
        ontology_path = conn_dir / ontology_file
        if ontology_path.exists():
            try:
                session.ontology_file = ontology_file
                restored.append("Ontology file")

                # Also read content for loaded_ontology state
                ontology_content = ontology_path.read_text(encoding="utf-8")
                session.loaded_ontology = ontology_content
                session.loaded_ontology_path = str(ontology_path)
            except Exception as e:
                logger.error(f"Failed to restore ontology: {e}")
                failed.append(f"Ontology: {e}")
        else:
            failed.append(f"Ontology file missing: {ontology_file}")

    # Restore R2RML file reference
    r2rml_file = schema_section.get("r2rml_file")
    if r2rml_file and (conn_dir / r2rml_file).exists():
        session.r2rml_file = r2rml_file

    # 3. Restore Oxigraph RDF store
    rdf_store = workspace.get("rdf_store", {})
    if rdf_store.get("initialized") and OXIGRAPH_AVAILABLE:
        try:
            store = get_oxigraph_store(ctx)
            if store:
                graph_uri = ontology_section.get("graph_uri")
                if graph_uri:
                    # Verify graph exists in store
                    try:
                        graph_data = store.export_graph(graph_uri, format="turtle")
                        if graph_data and len(graph_data) > 100:
                            restored.append(f"RDF store (graph: {graph_uri})")
                        else:
                            failed.append("RDF store: graph empty or not found")
                    except Exception:
                        failed.append("RDF store: graph verification failed")
                else:
                    restored.append("RDF store (initialized)")
        except Exception as e:
            logger.error(f"Failed to restore RDF store: {e}")
            failed.append(f"RDF store: {e}")

    # 4. Restore GraphRAG
    graphrag_section = schema_data.get("graphrag", {})
    if graphrag_section.get("initialized"):
        try:
            manager = GraphRAGManager(
                embedding_model="tfidf",
                embedding_dimension=384,
                connection_id=connection_id,
                schema_name=schema_name,
            )
            if manager.load_state(ensure_output_dir()):
                session.graphrag_manager = manager
                session.graphrag_initialized = True
                stats = manager.vector_store.get_statistics()
                restored.append(
                    f"GraphRAG: {stats.get('total_elements', 0)} embeddings, "
                    f"{manager.graph_retriever.graph.number_of_nodes()} tables"
                )
            else:
                failed.append("GraphRAG: load_state returned False")
        except Exception as e:
            logger.error(f"Failed to restore GraphRAG: {e}")
            failed.append(f"GraphRAG: {e}")

    # Build response
    result = "# Workspace Restored\n\n"
    result += f"Connection: {connection_id[:8]}...\n"
    result += f"Schema: {schema_name}\n\n"

    if restored:
        result += "## Restored\n"
        for item in restored:
            result += f"- {item}\n"

    if failed:
        result += "\n## Not Restored\n"
        for item in failed:
            result += f"- {item}\n"
        result += "\nUse the relevant tools to regenerate missing components.\n"

    if not restored and not failed:
        result += "No artifacts found to restore.\n"

    result += "\n## Ready to Use\n"
    if "Schema analysis" in str(restored):
        result += "- suggest_semantic_names() or generate_ontology()\n"
    if "Ontology" in str(restored):
        result += "- validate_sql_syntax() with ontology-aware validation\n"
    if "GraphRAG" in str(restored):
        result += "- graphrag_search() for semantic schema search\n"

    return result
