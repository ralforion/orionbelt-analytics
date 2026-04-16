"""Workspace restore, cleanup, and semantic model storage handler implementation."""

import json
import logging
import shutil
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastmcp import Context

from ..database_manager import TableInfo
from ..graphrag import GraphRAGManager
from ..lifecycle.metadata import VersionMetadataManager, update_workspace_section
from ..oxigraph_store import OXIGRAPH_AVAILABLE
from ..paths import OUTPUT_DIR, get_connection_dir, get_models_dir, ensure_output_dir

logger = logging.getLogger(__name__)


async def _restore_workspace_core(
    ctx: Context,
    session,
    connection_id: str,
    schema_name: Optional[str],
    get_oxigraph_store,
) -> Optional[Dict[str, Any]]:
    """Core workspace restore logic shared by connect_database and cleanup recovery.

    Loads schema cache, ontology, GraphRAG, and RDF store from disk into
    the session. Returns a structured result dict, or None if no workspace
    or schemas exist.

    Args:
        ctx: FastMCP context
        session: SessionData instance (already resolved)
        connection_id: Database connection fingerprint
        schema_name: Schema to restore (auto-selects first if None)
        get_oxigraph_store: Function to get/init Oxigraph store

    Returns:
        Dict with restore results, or None if workspace is empty/missing.
    """
    conn_dir = get_connection_dir(connection_id)

    # Load workspace metadata
    mgr = VersionMetadataManager(connection_id, OUTPUT_DIR)
    workspace = mgr.get_workspace()

    if not workspace:
        return None

    schemas = workspace.get("schemas", {})
    if not schemas:
        return None

    # Resolve schema name
    all_schemas = list(schemas.keys())
    if not schema_name:
        schema_name = all_schemas[0]
        if len(all_schemas) > 1:
            await ctx.info(
                f"Multiple schemas available: {', '.join(all_schemas)}. "
                f"Auto-restoring '{schema_name}'."
            )

    schema_data = schemas.get(schema_name)
    if not schema_data:
        return None

    restored: List[str] = []
    failed: List[str] = []

    # 1. Restore schema cache
    schema_section = schema_data.get("schema", {})
    schema_file = schema_section.get("schema_file")
    if schema_file:
        schema_path = conn_dir / schema_file
        if schema_path.exists():
            try:
                with open(schema_path, "r", encoding="utf-8") as f:
                    schema_json = json.load(f)

                tables_raw = schema_json.get("tables", [])
                tables_info = [TableInfo.from_dict(t) for t in tables_raw]

                session.cache_schema_analysis(schema_name, tables_info)
                session.schema_file = schema_file
                restored.append(f"Schema analysis: {len(tables_info)} tables")
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

    # Collect semantic models
    models = workspace.get("models", {})

    return {
        "schema_name": schema_name,
        "all_schemas": all_schemas,
        "restored": restored,
        "failed": failed,
        "ontology_enriched": session.ontology_enriched,
        "models": models,
    }


def _format_restore_summary(result: Dict[str, Any]) -> str:
    """Format a restore result dict into a user-facing markdown summary.

    Args:
        result: Dict from _restore_workspace_core()

    Returns:
        Formatted markdown string
    """
    schema_name = result["schema_name"]
    restored = result["restored"]
    failed = result["failed"]
    ontology_enriched = result.get("ontology_enriched", False)
    models = result.get("models", {})
    all_schemas = result.get("all_schemas", [schema_name])

    lines = ["# Workspace Auto-Restored", ""]
    lines.append(f"Schema: {schema_name}")

    if len(all_schemas) > 1:
        others = [s for s in all_schemas if s != schema_name]
        lines.append(f"Other schemas available: {', '.join(others)}")
        lines.append("Use analyze_schema(schema_name) to switch schemas.")
    lines.append("")

    if restored:
        lines.append("## Restored")
        for item in restored:
            lines.append(f"- {item}")

    if failed:
        lines.append("")
        lines.append("## Not Restored")
        for item in failed:
            lines.append(f"- {item}")
        lines.append("")
        lines.append("Use the relevant tools to regenerate missing components.")

    if not restored and not failed:
        lines.append("No artifacts found to restore.")

    # Build "do not call" list
    skip_tools = []
    if "Schema analysis" in str(restored):
        skip_tools.append("analyze_schema()")
    if "Ontology" in str(restored):
        skip_tools.append("generate_ontology()")
        if ontology_enriched:
            skip_tools.append("suggest_semantic_names()")
            skip_tools.append("apply_semantic_names()")
    if skip_tools:
        lines.append("")
        lines.append("## DO NOT CALL (already restored)")
        for tool in skip_tools:
            lines.append(f"- {tool}")

    lines.append("")
    lines.append("## Ready to Use")
    if "Schema analysis" in str(restored):
        if not ontology_enriched and "Ontology" not in str(restored):
            lines.append("- generate_ontology() to create ontology from cached schema")
        if not ontology_enriched and "Ontology" in str(restored):
            lines.append("- suggest_semantic_names() to enrich the ontology")
    if "Ontology" in str(restored):
        lines.append("- query_sparql() for semantic queries")
        lines.append("- validate_sql_syntax() with ontology-aware validation")
        lines.append("- execute_sql_query() for data queries")
    if "GraphRAG" in str(restored):
        lines.append("- graphrag_search() for semantic schema search")

    if models:
        lines.append("")
        lines.append("## Semantic Models Available")
        for model_name, model_info in models.items():
            saved_at = model_info.get("saved_at", "unknown")
            model_schema = model_info.get("schema_name", "")
            lines.append(
                f"- **{model_name}** (schema: {model_schema}, saved: {saved_at})"
            )
        lines.append("")
        lines.append("Use get_semantic_model(model_name) to retrieve model YAML.")

    return "\n".join(lines)


async def cleanup_workspace(
    ctx: Context,
    get_session_data,
    create_error_response,
) -> str:
    """Delete all workspace files for the current connection and clear session state.

    Removes schema JSON, ontology TTL, R2RML mappings, GraphRAG data,
    ChromaDB vectors, Oxigraph RDF store, semantic models, and metadata.
    The database connection itself remains active.

    Args:
        ctx: FastMCP context
        get_session_data: Function to get session data
        create_error_response: Function to create error response

    Returns:
        Summary of what was removed
    """
    session = get_session_data(ctx)

    if not session.connection_id:
        return create_error_response(
            "No database connection. Call connect_database first.",
            "connection_error",
        )

    connection_id = session.connection_id
    removed = []

    # 1. Close live resources before deleting their files
    if session.oxigraph_store is not None:
        try:
            session.oxigraph_store.close()
        except Exception as e:
            logger.debug(f"Oxigraph close during cleanup: {e}")

    # Drop GraphRAG reference (releases ChromaDB handle)
    session.graphrag_manager = None

    # 2. Delete workspace directories
    dirs_to_remove = [
        (OUTPUT_DIR / connection_id, "workspace"),
        (OUTPUT_DIR / "oxigraph" / connection_id, "Oxigraph RDF store"),
        (OUTPUT_DIR / "chromadb" / connection_id, "ChromaDB vector store"),
    ]
    for dir_path, label in dirs_to_remove:
        if dir_path.exists():
            try:
                shutil.rmtree(dir_path, ignore_errors=True)
                removed.append(label)
                logger.info(f"Cleaned up {label}: {dir_path}")
            except Exception as e:
                logger.warning(f"Failed to remove {label}: {e}")

    # 3. Clear all in-memory session state (keep connection alive)
    session.clear_schema_cache()
    session.schema_file = None
    session.ontology_file = None
    session.r2rml_file = None
    session.loaded_ontology = None
    session.loaded_ontology_path = None
    session.ontology_enriched = False
    session.obqc_validator = None
    session.oxigraph_store = None
    session.oxigraph_initialized = False
    session.graphrag_manager = None
    session.graphrag_initialized = False

    await ctx.info(f"Workspace cleaned for connection {connection_id[:8]}...")

    # 4. Build response
    result = "# Workspace Cleaned\n\n"
    result += f"Connection: {connection_id[:8]}... (still active)\n\n"

    if removed:
        result += "## Removed\n"
        for item in removed:
            result += f"- {item}\n"
    else:
        result += "No workspace files found to remove.\n"

    result += "\n## Session State Cleared\n"
    result += "- Schema cache, ontology, GraphRAG, RDF store\n\n"
    result += "Call analyze_schema() to start building a new workspace.\n"

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
