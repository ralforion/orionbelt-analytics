"""Schema analysis, table details, and cache management handler implementations."""

import asyncio
import json
import logging
import os
from typing import Optional, Dict, List, Any

from fastmcp import Context

from ..exceptions import ConnectionError, ParameterError
from ..paths import ensure_output_dir
from ..r2rml_generator import R2RMLGenerator

logger = logging.getLogger(__name__)


async def reset_cache(
    ctx: Context,
    cache_type: Optional[str],
    get_session_data,
) -> Dict[str, Any]:
    """Reset cached schema and/or ontology data to force re-analysis.

    Args:
        ctx: FastMCP context
        cache_type: Type of cache to reset ("schema", "ontology", "all", or None)
        get_session_data: Function to get session data
    """
    session = get_session_data(ctx)
    cleared = []

    cache_type_lower = (cache_type or "all").lower()

    if cache_type_lower in ("schema", "all"):
        session.clear_schema_cache()
        session.schema_file = None
        session.r2rml_file = None
        session.schema_cache._last_analyzed_schema = None
        cleared.append("schema")

    if cache_type_lower in ("ontology", "all"):
        session.ontology_file = None
        session.loaded_ontology = None
        session.obqc_validator = None
        cleared.append("ontology")

    await ctx.info(f"Cache cleared: {', '.join(cleared)}")

    return {
        "status": "success",
        "cleared_caches": cleared,
        "message": f"Cleared {', '.join(cleared)} cache(s). You can now re-run analyze_schema and/or generate_ontology.",
        "next_steps": {
            "schema": "Call analyze_schema() to re-analyze database schema",
            "ontology": "Call generate_ontology() to regenerate ontology",
        },
    }


async def analyze_schema(
    ctx: Context,
    schema_name: Optional[str],
    lightweight: bool,
    get_session_data,
    get_session_db_manager,
    get_session_safe_filename,
    _auto_initialize_graphrag_background,
) -> Dict[str, Any]:
    """Analyze database schema and return table metadata with relationships.

    Args:
        ctx: FastMCP context
        schema_name: Schema to analyze
        lightweight: If True, return minimal data
        get_session_data: Function to get session data
        get_session_db_manager: Function to get session db manager
        get_session_safe_filename: Function to generate safe filenames
        _auto_initialize_graphrag_background: Background init function
    """
    # Log function entry to verify code is being called
    logger.debug(f"analyze_schema() called - schema: '{schema_name}', lightweight: {lightweight}")

    # Check cache
    session = get_session_data(ctx)
    effective_schema = schema_name or ""
    cached_tables = session.get_cached_schema(effective_schema)

    logger.debug(f"Cache check result - cached_tables: {bool(cached_tables)} (count: {len(cached_tables) if cached_tables else 0})")

    if cached_tables:
        ontology_also_cached = session.ontology_file is not None

        # Auto-initialize GraphRAG even for cached results (if not already initialized)
        auto_graphrag = os.getenv("AUTO_GRAPHRAG", "true").lower()

        # Debug logging to diagnose auto-init issues
        logger.debug(f"GraphRAG auto-init check (CACHED path):")
        logger.debug(f"  AUTO_GRAPHRAG env: '{auto_graphrag}'")
        logger.debug(f"  cached_tables exists: {bool(cached_tables)} (count: {len(cached_tables) if cached_tables else 0})")
        logger.info(f"  session.graphrag_initialized: {session.graphrag_initialized}")
        logger.debug(f"  Will trigger: {auto_graphrag == 'true' and cached_tables and not session.graphrag_initialized}")

        if auto_graphrag == "true" and cached_tables and not session.graphrag_initialized:
            logger.info(f"GraphRAG auto-init triggered for cached schema: {effective_schema or 'default'}")
            task = asyncio.create_task(
                _auto_initialize_graphrag_background(
                    schema_name=effective_schema or "default",
                    tables_info=cached_tables,
                    session=session,
                    ctx=ctx,
                )
            )
            session.graphrag._init_task = task
            logger.info("GraphRAG auto-initialization started in background (from cache)")

        if ontology_also_cached:
            await ctx.info(
                "Schema AND ontology already cached - proceed directly to suggest_semantic_names()"
            )
            result = {
                "schema": effective_schema or "default",
                "table_count": len(cached_tables),
                "cache_hit": True,
                "ontology_cached": True,
                "message": "STOP! Both schema AND ontology are already CACHED. For enrichment, call suggest_semantic_names() directly!",
                "schema_file": session.schema_file,
                "ontology_file": session.ontology_file,
                "next_step": "suggest_semantic_names",
                "instruction": "Call suggest_semantic_names() NOW - do NOT call any other tools first!",
            }
            if auto_graphrag == "true":
                result["graphrag_auto_init"] = "started in background (from cache)"
            return result
        else:
            await ctx.info(
                f"Schema cached with {len(cached_tables)} tables - proceed to generate_ontology()"
            )
            result = {
                "schema": effective_schema or "default",
                "table_count": len(cached_tables),
                "cache_hit": True,
                "message": f"Schema already CACHED ({len(cached_tables)} tables). Call generate_ontology() next.",
                "schema_file": session.schema_file,
                "next_step": "generate_ontology",
                "instruction": "Call generate_ontology() NOW - do NOT call analyze_schema again!",
            }
            if auto_graphrag == "true":
                result["graphrag_auto_init"] = "started in background (from cache)"
            return result

    db_manager = get_session_db_manager(ctx)
    tables = db_manager.get_tables(schema_name)

    # Prefetch PKs and FKs at schema level (Snowflake optimization)
    if schema_name:
        db_manager.prefetch_schema_constraints(schema_name)

    # LIGHTWEIGHT MODE
    if lightweight:
        logger.info(f"Analyzing schema in LIGHTWEIGHT mode - {len(tables)} tables")

        table_info_objects = []
        relationships = {}
        fan_trap_warnings = []

        for table_name in tables:
            try:
                table_info = db_manager.analyze_table(table_name, schema_name)
                if table_info:
                    table_info_objects.append(table_info)

                    if table_info.foreign_keys:
                        relationships[table_name] = table_info.foreign_keys

                        if len(table_info.foreign_keys) > 1:
                            referenced_tables = [
                                fk["referenced_table"] for fk in table_info.foreign_keys
                            ]
                            fan_trap_warnings.append(
                                {
                                    "table": table_name,
                                    "warning": f"Table {table_name} connects to multiple tables - potential fan-trap risk",
                                    "referenced_tables": referenced_tables,
                                    "recommendation": "Use separate CTEs or UNION approach for multi-fact aggregations",
                                }
                            )
            except Exception as e:
                logger.warning(f"Failed to analyze table {table_name}: {e}")

        session.cache_schema_analysis(schema_name or "", table_info_objects)
        logger.info(f"Cached {len(table_info_objects)} tables for generate_ontology() reuse")

        lightweight_result = {
            "schema": schema_name or "default",
            "table_count": len(tables),
            "table_names": tables,
            "relationships": relationships,
            "mode": "lightweight",
            "token_savings": f"~{len(tables) * 85}% tokens saved vs full schema",
            "note": "Use get_table_details(table_name) to get column details on-demand",
            "next_step": "generate_ontology",
            "cache_hint": "Schema is now CACHED. Call generate_ontology() next - it will use cached data automatically.",
        }

        if fan_trap_warnings:
            lightweight_result["fan_trap_warnings"] = fan_trap_warnings

        # Auto-initialize GraphRAG in background
        auto_graphrag = os.getenv("AUTO_GRAPHRAG", "true").lower()

        # Debug logging to diagnose auto-init issues
        logger.debug(f"GraphRAG auto-init check (NON-CACHED path):")
        logger.debug(f"  AUTO_GRAPHRAG env: '{auto_graphrag}'")
        logger.debug(f"  table_info_objects exists: {bool(table_info_objects)} (count: {len(table_info_objects) if table_info_objects else 0})")
        logger.debug(f"  Will trigger: {auto_graphrag == 'true' and bool(table_info_objects)}")

        if auto_graphrag == "true" and table_info_objects:
            logger.info(f"GraphRAG auto-init triggered for schema: {schema_name or 'default'}")
            task = asyncio.create_task(
                _auto_initialize_graphrag_background(
                    schema_name=schema_name or "default",
                    tables_info=table_info_objects,
                    session=session,
                    ctx=ctx,
                )
            )
            session.graphrag._init_task = task
            logger.info("GraphRAG auto-initialization started in background")
            lightweight_result["graphrag_auto_init"] = "started in background"

        await ctx.info(
            f"Lightweight schema analysis: {len(tables)} tables cached, {len(relationships)} with FKs. Next: generate_ontology()"
        )
        return lightweight_result

    # FULL MODE
    all_table_info = []
    table_info_objects = []
    for table_name in tables:
        table_info = db_manager.analyze_table(table_name, schema_name)
        if table_info:
            table_info_objects.append(table_info)
            table_dict = {
                "name": table_info.name,
                "schema": table_info.schema,
                "columns": [
                    {
                        "name": col.name,
                        "data_type": col.data_type,
                        "is_nullable": col.is_nullable,
                        "is_primary_key": col.is_primary_key,
                        "is_foreign_key": col.is_foreign_key,
                        "foreign_key_table": col.foreign_key_table,
                        "foreign_key_column": col.foreign_key_column,
                        "comment": col.comment,
                    }
                    for col in table_info.columns
                ],
                "primary_keys": table_info.primary_keys,
                "foreign_keys": table_info.foreign_keys,
                "comment": table_info.comment,
                "row_count": table_info.row_count,
            }
            all_table_info.append(table_dict)

    schema_result = {
        "schema": schema_name or "default",
        "table_count": len(all_table_info),
        "tables": all_table_info,
    }

    session = get_session_data(ctx)
    session.cache_schema_analysis(schema_name or "", table_info_objects)

    # Save schema analysis to output folder
    schema_filename = None
    try:
        output_dir = ensure_output_dir()
        schema_safe = (schema_name or "default").replace(" ", "_").replace(".", "_")
        schema_filename = get_session_safe_filename(ctx, "schema", schema_safe) + ".json"
        schema_file_path = output_dir / schema_filename

        with open(schema_file_path, "w", encoding="utf-8") as f:
            json.dump(schema_result, f, indent=2, ensure_ascii=False)

        logger.info(f"Saved schema analysis to: {schema_file_path}")
        get_session_data(ctx).schema_file = schema_filename
        schema_result["schema_file"] = schema_filename
    except Exception as e:
        logger.warning(f"Failed to save schema analysis to file: {e}")

    # Generate R2RML mapping
    if table_info_objects:
        try:
            output_dir = ensure_output_dir()
            from ..constants import DEFAULT_R2RML_BASE_IRI

            effective_schema = schema_name or "default"
            r2rml_base = os.getenv("R2RML_BASE_IRI", DEFAULT_R2RML_BASE_IRI)
            if not r2rml_base.endswith("/"):
                r2rml_base += "/"
            base_iri = f"{r2rml_base}{effective_schema}/"

            database_name = db_manager.connection_info.get("database", "database")

            r2rml_generator = R2RMLGenerator(base_iri=base_iri, database_name=database_name)
            r2rml_content = r2rml_generator.generate_from_schema(
                table_info_objects, schema_name=effective_schema
            )

            schema_safe = effective_schema.replace(" ", "_").replace(".", "_")
            r2rml_filename = get_session_safe_filename(ctx, "r2rml", schema_safe) + ".ttl"
            r2rml_file_path = output_dir / r2rml_filename

            with open(r2rml_file_path, "w", encoding="utf-8") as f:
                f.write(r2rml_content)

            logger.info(f"Generated R2RML mapping: {r2rml_file_path}")
            get_session_data(ctx).r2rml_file = r2rml_filename
            schema_result["r2rml_file"] = r2rml_filename
            schema_result["r2rml_base_iri"] = base_iri

            await ctx.info(f"R2RML mapping generated with {len(table_info_objects)} tables")
        except Exception as e:
            logger.warning(f"Failed to generate R2RML mapping: {e}")
            schema_result["r2rml_error"] = str(e)

    # Add workflow guidance
    if all_table_info:
        schema_result["next_steps"] = {
            "recommended": "generate_ontology",
            "reason": "Generate ontology with database schema linking for accurate SQL generation and fan-trap prevention",
            "workflow": [
                "1. analyze_schema (completed - schema is now CACHED)",
                "2. generate_ontology (recommended next - will use cached schema automatically)",
                "3. execute_sql_query (with ontology context)",
            ],
        }
        schema_result["schema_cached"] = True
        schema_result["cache_hint"] = (
            "IMPORTANT: Schema analysis is now CACHED for this session. "
            "Do NOT call analyze_schema again - just call generate_ontology() directly. "
            "It will automatically use the cached schema data."
        )
        schema_result["analytical_guidance"] = (
            "Recommended next step: Run generate_ontology() - NO parameters needed!\n\n"
            "The schema is CACHED - generate_ontology will use it automatically.\n"
            "Do NOT call analyze_schema again.\n\n"
            "This will create an ontology with:\n"
            "- Database schema linking (db: namespace)\n"
            "- SQL column references for queries\n"
            "- JOIN conditions for relationships\n"
            "- Metadata for fan-trap prevention\n\n"
            "The ontology provides context for accurate SQL generation."
        )
        schema_result["next_tool"] = "generate_ontology"
        await ctx.info(
            f"Schema CACHED with {len(all_table_info)} tables. Next: generate_ontology() - no need to pass schema data, it's cached!"
        )
    else:
        await ctx.info("Schema analysis found no tables")

    # Auto-initialize GraphRAG in background (FULL MODE path)
    auto_graphrag = os.getenv("AUTO_GRAPHRAG", "true").lower()

    # Debug logging to diagnose auto-init issues
    logger.debug(f"GraphRAG auto-init check (FULL MODE path):")
    logger.debug(f"  AUTO_GRAPHRAG env: '{auto_graphrag}'")
    logger.debug(f"  table_info_objects exists: {bool(table_info_objects)} (count: {len(table_info_objects) if table_info_objects else 0})")
    logger.debug(f"  Will trigger: {auto_graphrag == 'true' and bool(table_info_objects)}")

    if auto_graphrag == "true" and table_info_objects:
        logger.info(f"GraphRAG auto-init triggered for schema: {schema_name or 'default'}")
        task = asyncio.create_task(
            _auto_initialize_graphrag_background(
                schema_name=schema_name or "default",
                tables_info=table_info_objects,
                session=get_session_data(ctx),
                ctx=ctx,
            )
        )
        session = get_session_data(ctx)
        session.graphrag._init_task = task
        logger.info("GraphRAG auto-initialization started in background (full mode)")
        schema_result["graphrag_auto_init"] = "started in background"

    return schema_result


async def get_table_details(
    ctx: Context,
    table_name: str,
    schema_name: Optional[str],
    get_session_db_manager,
) -> Dict[str, Any]:
    """Get detailed metadata for a single table.

    Args:
        ctx: FastMCP context
        table_name: Name of the table to analyze
        schema_name: Schema containing the table
        get_session_db_manager: Function to get session db manager
    """
    db_manager = get_session_db_manager(ctx)

    try:
        table_info = db_manager.analyze_table(table_name, schema_name)

        if not table_info:
            await ctx.error(f"Table '{table_name}' not found in schema '{schema_name or 'default'}'")
            return {
                "success": False,
                "error": f"Table '{table_name}' not found",
                "table_name": table_name,
                "schema_name": schema_name or "default",
            }

        table_dict = {
            "success": True,
            "name": table_info.name,
            "schema": table_info.schema,
            "columns": [
                {
                    "name": col.name,
                    "data_type": col.data_type,
                    "is_nullable": col.is_nullable,
                    "is_primary_key": col.is_primary_key,
                    "is_foreign_key": col.is_foreign_key,
                    "foreign_key_table": col.foreign_key_table,
                    "foreign_key_column": col.foreign_key_column,
                    "comment": col.comment,
                }
                for col in table_info.columns
            ],
            "primary_keys": table_info.primary_keys,
            "foreign_keys": table_info.foreign_keys,
            "comment": table_info.comment,
            "row_count": table_info.row_count,
        }

        await ctx.info(f"Retrieved details for table '{table_name}': {len(table_info.columns)} columns")
        return table_dict

    except Exception as e:
        logger.error(f"Failed to get table details for {table_name}: {e}")
        await ctx.error(f"Failed to analyze table '{table_name}': {str(e)}")
        return {
            "success": False,
            "error": str(e),
            "table_name": table_name,
            "schema_name": schema_name or "default",
        }


async def sample_table_data(
    ctx: Context,
    table_name: str,
    schema_name: Optional[str],
    limit: int,
    get_session_db_manager,
) -> List[Dict[str, Any]]:
    """Sample data from a specific table for analysis.

    Args:
        ctx: FastMCP context
        table_name: Name of the table to sample
        schema_name: Schema containing the table
        limit: Maximum number of rows to return
        get_session_db_manager: Function to get session db manager
    """
    if not table_name:
        return [{"error": "Table name is required"}]

    if limit <= 0 or limit > 100:
        limit = 10

    db_manager = get_session_db_manager(ctx)
    sample_data = db_manager.sample_table_data(table_name, schema_name, limit)

    if sample_data and len(sample_data) > 0:
        await ctx.info(
            f"Sample data retrieved with {len(sample_data)} rows; explore data or continue with other analysis"
        )
    else:
        await ctx.info("No sample data found for table")

    return sample_data
