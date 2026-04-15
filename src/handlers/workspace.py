"""Workspace restore and semantic model storage handler implementation."""

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastmcp import Context

from ..database_manager import TableInfo
from ..graphrag import GraphRAGManager
from ..lifecycle.metadata import VersionMetadataManager, update_workspace_section
from ..oxigraph_store import OXIGRAPH_AVAILABLE
from ..paths import OUTPUT_DIR, get_connection_dir, get_models_dir, ensure_output_dir

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
                is_enriched = ontology_section.get("enriched", False)
                session.ontology_enriched = is_enriched
                enriched_tag = " (enriched)" if is_enriched else ""
                restored.append(f"Ontology file{enriched_tag}")

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

    # Build "do not call" list based on what was restored
    skip_tools = []
    if "Schema analysis" in str(restored):
        skip_tools.append("analyze_schema()")
    if "Ontology" in str(restored):
        skip_tools.append("generate_ontology()")
        if session.ontology_enriched:
            skip_tools.append("suggest_semantic_names()")
            skip_tools.append("apply_semantic_names()")
    if skip_tools:
        result += "\n## DO NOT CALL (already restored)\n"
        for tool in skip_tools:
            result += f"- {tool}\n"

    result += "\n## Ready to Use\n"
    if "Schema analysis" in str(restored):
        if not session.ontology_enriched and "Ontology" not in str(restored):
            result += "- generate_ontology() to create ontology from cached schema\n"
        if not session.ontology_enriched and "Ontology" in str(restored):
            result += "- suggest_semantic_names() to enrich the ontology\n"
    if "Ontology" in str(restored):
        result += "- query_sparql() for semantic queries\n"
        result += "- validate_sql_syntax() with ontology-aware validation\n"
        result += "- execute_sql_query() for data queries\n"
    if "GraphRAG" in str(restored):
        result += "- graphrag_search() for semantic schema search\n"

    # 5. List available semantic models (names only, no content)
    models_section = workspace.get("models", {})
    if models_section:
        result += "\n## Semantic Models Available\n"
        for model_name, model_info in models_section.items():
            saved_at = model_info.get("saved_at", "unknown")
            model_schema = model_info.get("schema_name", "")
            result += f"- **{model_name}** (schema: {model_schema}, saved: {saved_at})\n"
        result += "\nUse get_semantic_model(model_name) to retrieve model YAML.\n"

    return result


async def save_semantic_model(
    ctx: Context,
    model_yaml: str,
    model_name: str,
    schema_name: Optional[str],
    get_session_data,
    create_error_response,
) -> Dict[str, Any]:
    """Save a semantic model YAML to the workspace for reuse across sessions.

    Args:
        ctx: FastMCP context
        model_yaml: The model definition in YAML format (e.g., OBML)
        model_name: Name to identify this model
        schema_name: Database schema this model is based on
        get_session_data: Function to get session data
        create_error_response: Function to create error response

    Returns:
        Save status with file path
    """
    session = get_session_data(ctx)

    if not session.connection_id:
        return create_error_response(
            "No database connection. Call connect_database first.",
            "connection_error",
        )

    connection_id = session.connection_id
    effective_schema = schema_name or session.get_last_analyzed_schema() or "default"

    # Save model file
    models_dir = get_models_dir(connection_id)
    safe_name = model_name.replace(" ", "_").replace("/", "_")
    model_filename = f"{safe_name}.yaml"
    model_path = models_dir / model_filename

    try:
        with open(model_path, "w", encoding="utf-8") as f:
            f.write(model_yaml)
        logger.info(f"Saved semantic model '{model_name}' to: {model_path}")
    except Exception as e:
        logger.error(f"Failed to save semantic model: {e}")
        return create_error_response(
            f"Failed to save model: {e}",
            "file_error",
        )

    # Update workspace metadata
    try:
        mgr = VersionMetadataManager(connection_id, OUTPUT_DIR)
        workspace = mgr.metadata.setdefault("workspace", {
            "updated_at": datetime.now().isoformat(),
            "schemas": {},
        })
        models = workspace.setdefault("models", {})
        models[model_name] = {
            "file": model_filename,
            "schema_name": effective_schema,
            "saved_at": datetime.now().isoformat(),
        }
        workspace["updated_at"] = datetime.now().isoformat()
        mgr._save_metadata()
    except Exception as e:
        logger.warning(f"Failed to update workspace metadata for model: {e}")

    await ctx.info(f"Saved semantic model '{model_name}' for schema '{effective_schema}'")

    return {
        "success": True,
        "model_name": model_name,
        "schema_name": effective_schema,
        "file": model_filename,
        "message": f"Model '{model_name}' saved. Use get_semantic_model('{model_name}') to retrieve it in future sessions.",
    }


async def get_semantic_model(
    ctx: Context,
    model_name: str,
    get_session_data,
    create_error_response,
) -> Dict[str, Any]:
    """Retrieve a stored semantic model YAML by name.

    Args:
        ctx: FastMCP context
        model_name: Name of the model to retrieve
        get_session_data: Function to get session data
        create_error_response: Function to create error response

    Returns:
        Model YAML content and metadata
    """
    session = get_session_data(ctx)

    if not session.connection_id:
        return create_error_response(
            "No database connection. Call connect_database first.",
            "connection_error",
        )

    connection_id = session.connection_id

    # Look up model in workspace metadata
    mgr = VersionMetadataManager(connection_id, OUTPUT_DIR)
    workspace = mgr.get_workspace()

    if not workspace:
        return create_error_response(
            "No workspace found for this connection.",
            "workspace_not_found",
        )

    models = workspace.get("models", {})
    model_info = models.get(model_name)

    if not model_info:
        available = list(models.keys())
        return create_error_response(
            f"Model '{model_name}' not found. Available models: {available or 'none'}",
            "model_not_found",
        )

    # Read model file
    model_filename = model_info["file"]
    models_dir = get_models_dir(connection_id)
    model_path = models_dir / model_filename

    if not model_path.exists():
        return create_error_response(
            f"Model file missing: {model_filename}",
            "file_not_found",
        )

    try:
        model_yaml = model_path.read_text(encoding="utf-8")
    except Exception as e:
        return create_error_response(
            f"Failed to read model file: {e}",
            "file_error",
        )

    await ctx.info(f"Retrieved semantic model '{model_name}'")

    return {
        "success": True,
        "model_name": model_name,
        "schema_name": model_info.get("schema_name", ""),
        "saved_at": model_info.get("saved_at", ""),
        "model_yaml": model_yaml,
    }


async def list_semantic_models(
    ctx: Context,
    get_session_data,
    create_error_response,
) -> Dict[str, Any]:
    """List all stored semantic models for the current connection.

    Args:
        ctx: FastMCP context
        get_session_data: Function to get session data
        create_error_response: Function to create error response

    Returns:
        List of available models with metadata
    """
    session = get_session_data(ctx)

    if not session.connection_id:
        return create_error_response(
            "No database connection. Call connect_database first.",
            "connection_error",
        )

    mgr = VersionMetadataManager(session.connection_id, OUTPUT_DIR)
    workspace = mgr.get_workspace()

    if not workspace:
        return {"models": [], "count": 0}

    models = workspace.get("models", {})
    model_list = [
        {
            "model_name": name,
            "schema_name": info.get("schema_name", ""),
            "saved_at": info.get("saved_at", ""),
        }
        for name, info in models.items()
    ]

    return {"models": model_list, "count": len(model_list)}
