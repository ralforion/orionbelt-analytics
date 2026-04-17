"""Ontology generation, semantic names, and loading handler implementations."""

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, Union

from fastmcp import Context

from ..database_manager import TableInfo, ColumnInfo
from ..ontology_generator import OntologyGenerator
from ..lifecycle.metadata import update_workspace_section, update_workspace_rdf
from ..paths import ensure_output_dir, get_connection_dir, OUTPUT_DIR, PROJECT_ROOT
from ..oxigraph_store import OXIGRAPH_AVAILABLE

logger = logging.getLogger(__name__)


def _build_minimal_graph_summary(ontology_ttl: str) -> str:
    """Build a compact class-and-relationship summary from a Turtle ontology.

    Parses the TTL with rdflib and returns a human-readable summary listing
    class names and relationship edges — enough for an LLM to understand the
    schema structure without consuming the full serialization.
    """
    from rdflib import Graph, Namespace
    from rdflib.namespace import RDF, RDFS, OWL

    g = Graph()
    g.parse(data=ontology_ttl, format="turtle")

    oba_ns = None
    for prefix, ns in g.namespaces():
        if prefix == "oba":
            oba_ns = Namespace(str(ns))
            break

    # Collect class names
    class_names = []
    for subj in g.subjects(RDF.type, OWL.Class):
        if subj == OWL.Class:
            continue
        label = None
        for lbl in g.objects(subj, RDFS.label):
            label = str(lbl)
        if not label:
            label = str(subj).split("/")[-1]
        row_count = None
        if oba_ns:
            for rc in g.objects(subj, oba_ns.rowCount):
                row_count = str(rc)
        entry = label
        if row_count:
            entry += f" ({row_count} rows)"
        class_names.append(entry)

    # Collect relationships
    rels = []
    for subj in g.subjects(RDF.type, OWL.ObjectProperty):
        label = None
        for lbl in g.objects(subj, RDFS.label):
            label = str(lbl)
        if not label:
            label = str(subj).split("/")[-1]
        # Get source table from rdfs:domain (oba:tableName is not set on ObjectProperties)
        domain_name = None
        for dom in g.objects(subj, RDFS.domain):
            domain_name = str(dom).split("/")[-1]
        ref_table = None
        if oba_ns:
            for rt in g.objects(subj, oba_ns.referencedTable):
                ref_table = str(rt)
        if domain_name and ref_table:
            rels.append(f"  {domain_name} -> {ref_table}  ({label})")

    lines = ["\n## Minimal Graph Summary\n"]
    lines.append(f"### Classes ({len(class_names)})")
    for cn in sorted(class_names):
        lines.append(f"  - {cn}")
    lines.append(f"\n### Relationships ({len(rels)})")
    for r in sorted(set(rels)):
        lines.append(r)
    lines.append(
        "\nUse download_ontology() to retrieve the full Turtle serialization."
    )
    return "\n".join(lines)


async def generate_ontology(
    ctx: Context,
    schema_info: Optional[str],
    schema_name: Optional[str],
    base_uri: str,
    auto_persist: bool,
    graph_uri: Optional[str],
    get_session_data,
    get_session_db_manager,
    get_session_safe_filename,
    get_oxigraph_store,
    create_error_response,
    _server_state,
) -> str:
    """Generate an RDF ontology from database schema.

    Extracts implementation from main.py's generate_ontology tool.
    """
    # Resolve effective schema and set current schema for state isolation
    session = get_session_data(ctx)
    effective_schema_for_state = schema_name
    if not effective_schema_for_state:
        effective_schema_for_state = session.get_last_analyzed_schema()
    if effective_schema_for_state:
        session.set_current_schema(effective_schema_for_state)

    # Check if ontology is already generated
    if session.ontology_file:
        if session.ontology_enriched:
            await ctx.info("Ontology CACHED and already enriched — ready to use")
            return (
                f"# ONTOLOGY ALREADY CACHED AND ENRICHED\n\n"
                f"Ontology file: {session.ontology_file}\n\n"
                f"Semantic names have already been applied. The ontology is ready to use.\n\n"
                f"Do NOT call generate_ontology, analyze_schema, or suggest_semantic_names again.\n\n"
                f"## READY TO USE:\n"
                f"- query_sparql() for semantic queries\n"
                f"- execute_sql_query() for data queries (includes built-in validation)"
            )
        await ctx.info("Ontology CACHED - call suggest_semantic_names() for enrichment")
        return (
            f"# ONTOLOGY ALREADY CACHED\n\n"
            f"Ontology file: {session.ontology_file}\n\n"
            f"Do NOT call generate_ontology or analyze_schema again!\n\n"
            f"## FOR ENRICHMENT:\n"
            f"Call suggest_semantic_names() NOW - it will use the cached ontology automatically.\n\n"
            f"That's the ONLY tool you need to call for enrichment!"
        )

    # Validate base_uri
    if not base_uri.endswith("/"):
        base_uri += "/"

    tables_info = []

    if schema_info:
        # Use provided schema information
        try:
            schema_data = json.loads(schema_info) if isinstance(schema_info, str) else schema_info

            if "tables" in schema_data:
                for table_data in schema_data["tables"]:
                    columns = []
                    for col_data in table_data.get("columns", []):
                        column = ColumnInfo(
                            name=col_data["name"],
                            data_type=col_data["data_type"],
                            is_nullable=col_data.get("is_nullable", True),
                            is_primary_key=col_data.get("is_primary_key", False),
                            is_foreign_key=col_data.get("is_foreign_key", False),
                            foreign_key_table=col_data.get("foreign_key_table"),
                            foreign_key_column=col_data.get("foreign_key_column"),
                            comment=col_data.get("comment"),
                        )
                        columns.append(column)

                    table = TableInfo(
                        name=table_data["name"],
                        schema=table_data.get("schema", schema_name or "default"),
                        columns=columns,
                        primary_keys=table_data.get("primary_keys", []),
                        foreign_keys=table_data.get("foreign_keys", []),
                        comment=table_data.get("comment"),
                        row_count=table_data.get("row_count"),
                    )
                    tables_info.append(table)

            logger.info(f"Using provided schema info: {len(tables_info)} tables")

        except Exception as e:
            return create_error_response(
                f"Failed to parse schema_info parameter: {str(e)}", "parameter_error"
            )
    else:
        # Try to use cached schema analysis
        session = get_session_data(ctx)

        effective_schema = schema_name
        if not effective_schema:
            effective_schema = session.get_last_analyzed_schema()
            if effective_schema:
                logger.info(f"Using last analyzed schema: {effective_schema}")

        cached_tables = session.get_cached_schema(effective_schema or "")

        if cached_tables:
            schema_name = effective_schema
            tables_info = cached_tables
            logger.info(
                f"Using CACHED schema from analyze_schema: {len(tables_info)} tables (no re-query needed)"
            )
            await ctx.info(f"Using cached schema: {len(tables_info)} tables - no database queries needed")
        else:
            schema_name = effective_schema or schema_name
            db_manager = get_session_db_manager(ctx)

            if not db_manager.has_engine():
                return create_error_response(
                    "No database connection established and no schema_info provided. Please use connect_database tool first or provide schema_info parameter.",
                    "connection_error",
                )

            try:
                tables = db_manager.get_tables(schema_name)
                logger.info(f"Found {len(tables)} tables in schema '{schema_name or 'default'}': {tables}")

                if schema_name:
                    db_manager.prefetch_schema_constraints(schema_name)

                for table_name in tables:
                    try:
                        table_info = db_manager.analyze_table(table_name, schema_name)
                        if table_info:
                            tables_info.append(table_info)
                    except Exception as e:
                        logger.error(f"Failed to analyze table {table_name}: {e}")

                session.cache_schema_analysis(schema_name or "", tables_info)

            except Exception as e:
                return create_error_response(
                    f"Failed to get tables from database: {str(e)}", "database_error"
                )

    if not tables_info:
        return create_error_response("No tables found to generate ontology from", "data_error")

    generator = _server_state.get_ontology_generator(base_uri=base_uri)
    ontology_ttl = generator.generate_from_schema(tables_info)

    # Save ontology to connection-scoped output folder
    ontology_filename = None
    try:
        session = get_session_data(ctx)
        conn_dir = get_connection_dir(session.connection_id) if session.connection_id else ensure_output_dir()

        schema_safe = (schema_name or "default").replace(" ", "_").replace(".", "_")
        ontology_filename = get_session_safe_filename(ctx, "ontology", schema_safe) + ".ttl"
        ontology_file_path = conn_dir / ontology_filename

        with open(ontology_file_path, "w", encoding="utf-8") as f:
            f.write(ontology_ttl)

        logger.info(f"Generated ontology for schema '{schema_name or 'default'}': {len(tables_info)} tables")
        logger.info(f"Saved ontology to: {ontology_file_path}")

        session.ontology_file = ontology_filename
        session.obqc_validator = None

        # Write workspace metadata for ontology section
        if session.connection_id:
            try:
                await update_workspace_section(
                    connection_id=session.connection_id,
                    output_dir=OUTPUT_DIR,
                    schema_name=schema_name or "default",
                    section="ontology",
                    data={
                        "ontology_file": ontology_filename,
                        "enriched": False,
                        "graph_uri": graph_uri,
                        "persisted_to_rdf": False,
                        "generated_at": datetime.now().isoformat(),
                    },
                )
            except Exception as e:
                logger.warning(f"Failed to write workspace metadata: {e}")

        await ctx.info(
            "Ontology generation complete; next call should be suggest_semantic_names to improve cryptic names"
        )

        # Analyze for cryptic names
        generator = _server_state.get_ontology_generator(base_uri=base_uri)
        generator.graph.parse(data=ontology_ttl, format="turtle")
        generator.graph.bind("ns", generator.base_uri)
        generator.graph.bind("oba", generator.oba_ns)
        name_analysis = generator.extract_names_for_review()

        cryptic_count = (
            name_analysis["summary"]["classes_needing_review"]
            + name_analysis["summary"]["properties_needing_review"]
            + name_analysis["summary"]["relationships_needing_review"]
        )

        # Auto-persist to RDF store
        if auto_persist and OXIGRAPH_AVAILABLE:
            try:
                store = get_oxigraph_store(ctx)
                if store:
                    if not graph_uri:
                        schema_safe = (schema_name or "default").replace(" ", "_").replace(".", "_")
                        graph_uri = f"http://example.com/schema/{schema_safe}"

                    triple_count = store.load_ontology(ontology_ttl, graph_uri, schema_name or "default")
                    logger.info(
                        f"Auto-persisted ontology to Oxigraph: {triple_count} triples in graph <{graph_uri}>"
                    )

                    # Update workspace: mark ontology as persisted + write rdf_store
                    if session.connection_id:
                        try:
                            await update_workspace_section(
                                connection_id=session.connection_id,
                                output_dir=OUTPUT_DIR,
                                schema_name=schema_name or "default",
                                section="ontology",
                                data={
                                    "ontology_file": ontology_filename,
                                    "enriched": False,
                                    "graph_uri": graph_uri,
                                    "persisted_to_rdf": True,
                                    "generated_at": datetime.now().isoformat(),
                                },
                            )
                            await update_workspace_rdf(
                                connection_id=session.connection_id,
                                output_dir=OUTPUT_DIR,
                                data={
                                    "initialized": True,
                                    "graph_uris": [graph_uri],
                                    "initialized_at": datetime.now().isoformat(),
                                },
                            )
                        except Exception as e:
                            logger.warning(f"Failed to write workspace metadata: {e}")

                    result = f"""Ontology generated and stored successfully!

Schema: {schema_name or "default"}
Tables: {len(tables_info)}
Ontology file: {ontology_filename}
Storage location: {conn_dir}/
Graph URI: <{graph_uri}>
Triples stored: {triple_count:,}

Ontology is now persistent in Oxigraph RDF database.
Use query_sparql() to explore the schema graph.
Use download_ontology(schema_name="{schema_name or "default"}") to get the TTL file.

Token savings: ~{len(ontology_ttl)//4} tokens saved by auto-persisting to RDF store!"""

                    if cryptic_count > 0:
                        result += f"""

SEMANTIC NAME RESOLUTION RECOMMENDED
Found {cryptic_count} names that may need review:
  Classes needing review: {name_analysis['summary']['classes_needing_review']}
  Properties needing review: {name_analysis['summary']['properties_needing_review']}
  Relationships needing review: {name_analysis['summary']['relationships_needing_review']}

To improve ontology for business users:
1. Call suggest_semantic_names() (ontology is CACHED)
2. Review suggestions and provide alternatives
3. Call apply_semantic_names() with your suggestions"""

                    return result
            except Exception as e:
                logger.warning(f"Auto-persist to Oxigraph failed: {e}, falling back to full TTL return")

        # Fallback: return minimal graph summary instead of full TTL
        result = f"Ontology generated and saved to file: {ontology_filename}\n"
        result += f"Schema: {schema_name or 'default'}, Tables: {len(tables_info)}\n"
        result += _build_minimal_graph_summary(ontology_ttl)

        if cryptic_count > 0:
            result += f"\n\nSEMANTIC NAME RESOLUTION RECOMMENDED"
            result += f"\nFound {cryptic_count} names that may be abbreviations or cryptic identifiers."
            result += f"\n1. Call suggest_semantic_names() - NO parameters needed, ontology is CACHED"
            result += f"\n2. Review the suggestions and provide business-friendly alternatives"
            result += f"\n3. Call apply_semantic_names() with your suggestions"

        return result

    except Exception as e:
        logger.warning(f"Failed to save ontology to file: {e}")
        await ctx.info(
            "Ontology file save failed but ontology generated; next call should be suggest_semantic_names to improve cryptic names"
        )
        return _build_minimal_graph_summary(ontology_ttl)


async def suggest_semantic_names(
    ctx: Context,
    ontology_file: Optional[str],
    get_session_data,
    load_ontology_from_session,
) -> Dict[str, Any]:
    """Extract and analyze names from a generated ontology."""
    try:
        try:
            if ontology_file:
                session = get_session_data(ctx)
                file_dir = get_connection_dir(session.connection_id) if session.connection_id else ensure_output_dir()
                ontology_path = file_dir / ontology_file
                if not ontology_path.exists():
                    return {
                        "error": f"Ontology file not found: {ontology_file}",
                        "error_type": "file_not_found",
                        "hint": "Check the filename from generate_ontology response",
                    }
                generator = OntologyGenerator()
                generator.load_from_file(str(ontology_path))
                source_filename = ontology_file
                logger.info(f"Loaded ontology from provided file: {ontology_file}")
            else:
                generator, source_filename = load_ontology_from_session(ctx)
        except ValueError as e:
            return {
                "error": str(e),
                "error_type": "session_error",
                "hint": "Pass ontology_file parameter from generate_ontology response",
            }

        extraction_result = generator.extract_names_for_review(compact=True)

        # Build compact review lists — only cryptic items, grouped to save tokens
        cryptic_classes = [
            c["local_name"]
            for c in extraction_result["classes"]
            if c.get("needs_review", {}).get("is_cryptic")
        ]

        # Group cryptic properties by table for compact output
        cryptic_props_by_table: Dict[str, list] = {}
        for p in extraction_result["properties"]:
            if p.get("needs_review", {}).get("is_cryptic"):
                table = p.get("table_name") or "unknown"
                cryptic_props_by_table.setdefault(table, []).append(
                    p.get("column_name") or p["local_name"]
                )

        cryptic_relationships = [
            r["local_name"]
            for r in extraction_result["relationships"]
            if r.get("needs_review", {}).get("is_cryptic")
        ]

        total_cryptic = (
            len(cryptic_classes)
            + sum(len(v) for v in cryptic_props_by_table.values())
            + len(cryptic_relationships)
        )
        summary = extraction_result["summary"]

        await ctx.info(
            f"Found {total_cryptic} cryptic names to review; "
            f"next call should be apply_semantic_names with your suggestions"
        )

        return {
            "ontology_file": source_filename,
            "summary": summary,
            "cryptic_classes": cryptic_classes,
            "cryptic_properties_by_table": cryptic_props_by_table,
            "cryptic_relationships": cryptic_relationships,
            "next_step": "Review the cryptic names and call apply_semantic_names with your suggestions",
            "next_tool": "apply_semantic_names",
        }

    except Exception as e:
        logger.error(f"Error extracting names for review: {e}")
        return {
            "error": f"Failed to extract names: {str(e)}",
            "error_type": "internal_error",
        }


async def apply_semantic_names(
    ctx: Context,
    suggestions: Union[str, Dict[str, Any]],
    ontology_file: Optional[str],
    save_to_file: bool,
    get_session_data,
    get_session_safe_filename,
    load_ontology_from_session,
    create_error_response,
    get_oxigraph_store=None,
) -> str:
    """Apply LLM-suggested semantic names to an existing ontology."""
    try:
        session = get_session_data(ctx)
        try:
            if ontology_file:
                conn_dir = get_connection_dir(session.connection_id) if session.connection_id else ensure_output_dir()
                ontology_path = conn_dir / ontology_file
                if not ontology_path.exists():
                    return create_error_response(
                        f"Ontology file not found: {ontology_file}", "file_not_found"
                    )
                generator = OntologyGenerator()
                generator.load_from_file(str(ontology_path))
                source_filename = ontology_file
                logger.info(f"Loaded ontology from provided file: {ontology_file}")
            else:
                generator, source_filename = load_ontology_from_session(ctx)
        except ValueError as e:
            return create_error_response(
                f"{str(e)} - pass ontology_file parameter from generate_ontology response",
                "session_error",
            )

        try:
            if isinstance(suggestions, str):
                name_suggestions = json.loads(suggestions)
            else:
                name_suggestions = suggestions
        except json.JSONDecodeError as e:
            return create_error_response(
                f"Invalid JSON in suggestions parameter: {str(e)}",
                "parameter_error",
                "Ensure suggestions is valid JSON with classes, properties, and relationships arrays",
            )

        if not isinstance(name_suggestions, dict):
            return create_error_response(
                "Suggestions must be a JSON object with 'classes', 'properties', and/or 'relationships' arrays",
                "parameter_error",
            )

        updated_ontology = generator.apply_semantic_names(name_suggestions)

        new_ontology_filename = None
        if save_to_file:
            try:
                conn_dir = get_connection_dir(session.connection_id) if session.connection_id else ensure_output_dir()
                new_ontology_filename = get_session_safe_filename(ctx, "ontology", "semantic") + ".ttl"
                ontology_file_path = conn_dir / new_ontology_filename

                with open(ontology_file_path, "w", encoding="utf-8") as f:
                    f.write(updated_ontology)

                logger.info(f"Saved semantic ontology to: {ontology_file_path}")
                session.ontology_file = new_ontology_filename
                session.ontology_enriched = True
                session.obqc_validator = None

                # Update workspace: mark ontology as enriched
                if session.connection_id:
                    try:
                        schema_name = session.get_last_analyzed_schema() or "default"
                        await update_workspace_section(
                            connection_id=session.connection_id,
                            output_dir=OUTPUT_DIR,
                            schema_name=schema_name,
                            section="ontology",
                            data={
                                "ontology_file": new_ontology_filename,
                                "enriched": True,
                                "persisted_to_rdf": False,
                                "generated_at": datetime.now().isoformat(),
                            },
                        )
                    except Exception as e:
                        logger.warning(f"Failed to write workspace metadata: {e}")
            except Exception as e:
                logger.warning(f"Failed to save ontology to file: {e}")

        classes_updated = len(name_suggestions.get("classes", []))
        properties_updated = len(name_suggestions.get("properties", []))
        relationships_updated = len(name_suggestions.get("relationships", []))
        total_updated = classes_updated + properties_updated + relationships_updated

        await ctx.info(f"Applied {total_updated} semantic name changes to ontology")

        result = "# Semantic Names Applied Successfully\n\n"
        result += f"- Classes updated: {classes_updated}\n"
        result += f"- Properties updated: {properties_updated}\n"
        result += f"- Relationships updated: {relationships_updated}\n"
        if new_ontology_filename:
            result += f"\n## ontology_file: {new_ontology_filename}\n"
            result += f"\nThe ontology file '{new_ontology_filename}' has been saved and is now the active ontology in session context.\n"

        # Auto-persist to Oxigraph to avoid returning the full TTL
        persisted = False
        if OXIGRAPH_AVAILABLE and get_oxigraph_store is not None:
            try:
                store = get_oxigraph_store(ctx)
                if store:
                    session = get_session_data(ctx)
                    schema_name = getattr(session, "schema_name", "default") or "default"
                    schema_safe = schema_name.replace(" ", "_").replace(".", "_")
                    graph_uri = f"http://example.com/schema/{schema_safe}"
                    triple_count = store.load_ontology(updated_ontology, graph_uri, schema_name)
                    result += f"\nPersisted to Oxigraph: {triple_count:,} triples in <{graph_uri}>"
                    result += f"\nToken savings: ~{len(updated_ontology) // 4} tokens saved by auto-persisting to RDF store!"
                    result += "\nUse query_sparql() to explore or download_ontology() to get the TTL file."
                    persisted = True
            except Exception as e:
                logger.warning(f"Auto-persist semantic ontology to Oxigraph failed: {e}")

        if not persisted:
            # Without Oxigraph, return a minimal graph summary instead of full TTL
            result += _build_minimal_graph_summary(updated_ontology)

        return result

    except Exception as e:
        logger.error(f"Error applying semantic names: {e}")
        return create_error_response(f"Failed to apply semantic names: {str(e)}", "internal_error")


async def load_my_ontology(
    ctx: Context,
    import_folder: str,
    auto_persist: bool,
    graph_uri: Optional[str],
    get_session_data,
    get_oxigraph_store,
) -> Dict[str, Any]:
    """Load the newest .ttl ontology file from the import folder."""
    try:
        from rdflib import Graph
        from rdflib.namespace import RDF, OWL

        # Resolve import folder path
        if import_folder.startswith("./"):
            folder_path = PROJECT_ROOT / import_folder[2:]
        elif not os.path.isabs(import_folder):
            folder_path = PROJECT_ROOT / import_folder
        else:
            folder_path = Path(import_folder)

        if not folder_path.exists():
            return {
                "success": False,
                "error": f"Import folder not found: {folder_path}",
                "error_type": "folder_not_found",
                "suggestion": "Create the folder and add .ttl files, or specify a different path",
            }

        ttl_files = list(folder_path.glob("*.ttl"))
        if not ttl_files:
            return {
                "success": False,
                "error": f"No .ttl files found in: {folder_path}",
                "error_type": "no_files_found",
                "suggestion": "Add .ttl ontology files to the import folder",
            }

        ttl_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
        newest_file = ttl_files[0]

        with open(newest_file, "r", encoding="utf-8") as f:
            ontology_content = f.read()

        graph = Graph()
        try:
            graph.parse(data=ontology_content, format="turtle")
        except Exception as parse_error:
            return {
                "success": False,
                "error": f"Failed to parse ontology file: {str(parse_error)}",
                "error_type": "parse_error",
                "file_path": str(newest_file),
                "suggestion": "Ensure the file is valid Turtle format",
            }

        classes_count = len(list(graph.subjects(RDF.type, OWL.Class)))
        datatype_props = len(list(graph.subjects(RDF.type, OWL.DatatypeProperty)))
        object_props = len(list(graph.subjects(RDF.type, OWL.ObjectProperty)))

        file_stat = newest_file.stat()
        modified_time = datetime.fromtimestamp(file_stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")

        session = get_session_data(ctx)
        session.loaded_ontology = ontology_content
        session.loaded_ontology_path = str(newest_file)
        session.obqc_validator = None

        logger.info(f"Loaded ontology from: {newest_file}")
        logger.info(
            f"Ontology contains: {classes_count} classes, {datatype_props} data properties, {object_props} object properties"
        )

        # Auto-persist to RDF store
        stored_in_rdf = False
        triple_count = 0
        used_graph_uri = None

        if auto_persist and OXIGRAPH_AVAILABLE:
            try:
                store = get_oxigraph_store(ctx)
                if store:
                    schema_name = newest_file.stem.replace("ontology_", "")
                    if not graph_uri:
                        graph_uri = f"http://example.com/schema/{schema_name}"
                    used_graph_uri = graph_uri

                    triple_count = store.load_ontology(ontology_content, graph_uri, schema_name)
                    stored_in_rdf = True

                    logger.info(
                        f"Auto-persisted ontology to Oxigraph: {triple_count} triples in graph <{graph_uri}>"
                    )
                    await ctx.info(
                        f"Ontology loaded and stored in RDF database with {triple_count:,} triples; ready for SPARQL queries"
                    )
                else:
                    logger.warning("Oxigraph store not available for auto-persist")
                    await ctx.info(f"Ontology loaded with {classes_count} classes; ready for SQL generation")
            except Exception as e:
                logger.warning(f"Auto-persist to Oxigraph failed: {e}, ontology still available in session state")
                await ctx.info(f"Ontology loaded with {classes_count} classes; ready for SQL generation")
        else:
            await ctx.info(f"Ontology loaded with {classes_count} classes; ready for SQL generation")

        response = {
            "success": True,
            "file_path": str(newest_file),
            "file_name": newest_file.name,
            "file_size": file_stat.st_size,
            "modified_time": modified_time,
            "classes_count": classes_count,
            "properties_count": datatype_props,
            "relationships_count": object_props,
            "total_files_found": len(ttl_files),
            "other_files": [f.name for f in ttl_files[1:5]] if len(ttl_files) > 1 else [],
            "stored_in_rdf": stored_in_rdf,
        }

        if stored_in_rdf:
            response["graph_uri"] = used_graph_uri
            response["triples_stored"] = triple_count
            response["next_steps"] = {
                "recommended": "query_sparql",
                "reason": "The loaded ontology is now in Oxigraph RDF database",
                "workflow": [
                    "1. load_my_ontology (completed)",
                    "2. query_sparql (explore schema with SPARQL)",
                    "3. execute_sql_query (use ontology context for SQL generation)",
                ],
            }
            response["note"] = (
                f"Ontology stored in RDF database with {triple_count:,} triples. "
                f"Token savings: ~{len(ontology_content)//4} tokens!"
            )
        else:
            preview = ontology_content[:2000]
            if len(ontology_content) > 2000:
                preview += "\n\n... [truncated, full content available in file]"
            response["ontology_preview"] = preview
            response["next_steps"] = {
                "recommended": "execute_sql_query",
                "reason": "The loaded ontology provides semantic context for SQL generation",
                "workflow": [
                    "1. load_my_ontology (completed)",
                    "2. connect_database (if not already connected)",
                    "3. execute_sql_query (use ontology context for accurate SQL)",
                ],
            }
            response["note"] = "This ontology is now active and will be used instead of auto-generated ontologies"

        return response

    except Exception as e:
        logger.error(f"Error loading ontology: {e}")
        return {
            "success": False,
            "error": f"Failed to load ontology: {str(e)}",
            "error_type": "internal_error",
        }


async def download_ontology(
    ctx: Context,
    schema_name: Optional[str],
    source: str,
    get_session_data,
    get_oxigraph_store,
    create_error_response,
) -> Dict[str, Any]:
    """Download ontology as TTL file from RDF store or tmp folder."""
    try:
        session = get_session_data(ctx)

        if not schema_name:
            schema_name = session.get_last_analyzed_schema()
            if not schema_name:
                return {
                    "success": False,
                    "error": "No schema_name provided and no schema in session",
                    "error_type": "parameter_error",
                    "hint": "Provide schema_name parameter or generate/load an ontology first",
                }

        schema_safe = schema_name.replace(" ", "_").replace(".", "_")
        conn_dir = get_connection_dir(session.connection_id) if session.connection_id else ensure_output_dir()

        if source == "rdf" and OXIGRAPH_AVAILABLE:
            store = get_oxigraph_store(ctx)
            if not store:
                return {
                    "success": False,
                    "error": "Oxigraph RDF store not initialized",
                    "error_type": "rdf_error",
                    "hint": "Call store_ontology_in_rdf first or use source='file'",
                }

            graph_uri = f"http://example.com/schema/{schema_safe}"

            try:
                ontology_ttl = store.export_graph(graph_uri, format="turtle")

                if not ontology_ttl or len(ontology_ttl) < 100:
                    return {
                        "success": False,
                        "error": f"Graph <{graph_uri}> is empty or not found in RDF store",
                        "error_type": "rdf_error",
                        "hint": f"Call store_ontology_in_rdf(schema_name='{schema_name}') first",
                    }

                file_name = f"ontology_{schema_safe}_export.ttl"
                file_path = conn_dir / file_name

                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(ontology_ttl)

                triple_count = len(
                    [
                        line
                        for line in ontology_ttl.split("\n")
                        if line.strip() and not line.strip().startswith("#") and not line.strip().startswith("@")
                    ]
                )

                logger.info(f"Exported ontology from RDF store <{graph_uri}> to {file_path}")

                return {
                    "success": True,
                    "content": ontology_ttl,
                    "file_path": str(file_path),
                    "file_name": file_name,
                    "file_size": len(ontology_ttl),
                    "triple_count": triple_count,
                    "graph_uri": graph_uri,
                    "source": "rdf",
                    "note": f"Ontology exported from Oxigraph RDF store. File saved to: {file_path}",
                }
            except Exception as e:
                logger.error(f"Failed to export from RDF store: {e}")
                return {
                    "success": False,
                    "error": f"Failed to export from RDF store: {str(e)}",
                    "error_type": "rdf_error",
                    "hint": "Try source='file' to read from tmp folder instead",
                }

        elif source == "file":
            ontology_filename = session.ontology_file
            if not ontology_filename:
                pattern = f"ontology_{schema_safe}*.ttl"
                matching_files = list(conn_dir.glob(pattern))
                if matching_files:
                    matching_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
                    ontology_file_path = matching_files[0]
                else:
                    return {
                        "success": False,
                        "error": f"No ontology file found for schema '{schema_name}' in connection folder",
                        "error_type": "file_not_found",
                        "hint": "Generate ontology first with generate_ontology()",
                    }
            else:
                ontology_file_path = conn_dir / ontology_filename

            if not ontology_file_path.exists():
                return {
                    "success": False,
                    "error": f"Ontology file not found: {ontology_file_path}",
                    "error_type": "file_not_found",
                }

            with open(ontology_file_path, "r", encoding="utf-8") as f:
                ontology_ttl = f.read()

            file_stat = ontology_file_path.stat()
            logger.info(f"Read ontology from file: {ontology_file_path}")

            return {
                "success": True,
                "content": ontology_ttl,
                "file_path": str(ontology_file_path),
                "file_name": ontology_file_path.name,
                "file_size": file_stat.st_size,
                "source": "file",
                "note": f"Ontology read from tmp folder: {ontology_file_path}",
            }

        else:
            return {
                "success": False,
                "error": f"Invalid source: {source}. Must be 'rdf' or 'file'",
                "error_type": "parameter_error",
            }

    except Exception as e:
        logger.error(f"Error downloading ontology: {e}")
        return {
            "success": False,
            "error": f"Failed to download ontology: {str(e)}",
            "error_type": "internal_error",
        }


async def download_r2rml(
    ctx: Context,
    schema_name: Optional[str],
    get_session_data,
) -> Dict[str, Any]:
    """Download R2RML mapping file from tmp folder."""
    try:
        session = get_session_data(ctx)

        if not schema_name:
            schema_name = session.get_last_analyzed_schema()
            if not schema_name:
                return {
                    "success": False,
                    "error": "No schema_name provided and no schema in session",
                    "error_type": "parameter_error",
                    "hint": "Provide schema_name parameter or run analyze_schema() first",
                }

        schema_safe = schema_name.replace(" ", "_").replace(".", "_")
        conn_dir = get_connection_dir(session.connection_id) if session.connection_id else ensure_output_dir()

        r2rml_filename = session.r2rml_file
        if not r2rml_filename:
            pattern = f"r2rml_{schema_safe}*.ttl"
            matching_files = list(conn_dir.glob(pattern))
            if matching_files:
                matching_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
                r2rml_file_path = matching_files[0]
            else:
                return {
                    "success": False,
                    "error": f"No R2RML file found for schema '{schema_name}' in connection folder",
                    "error_type": "file_not_found",
                    "hint": "Run analyze_schema() first to generate R2RML mapping",
                }
        else:
            r2rml_file_path = conn_dir / r2rml_filename

        if not r2rml_file_path.exists():
            return {
                "success": False,
                "error": f"R2RML file not found: {r2rml_file_path}",
                "error_type": "file_not_found",
                "hint": "Run analyze_schema() to generate R2RML mapping",
            }

        with open(r2rml_file_path, "r", encoding="utf-8") as f:
            r2rml_content = f.read()

        file_stat = r2rml_file_path.stat()

        base_iri = "http://example.com/r2rml/"
        if "rr:baseIRI" in r2rml_content:
            match = re.search(r'rr:baseIRI\s+"([^"]+)"', r2rml_content)
            if match:
                base_iri = match.group(1)

        logger.info(f"Read R2RML mapping from file: {r2rml_file_path}")

        return {
            "success": True,
            "content": r2rml_content,
            "file_path": str(r2rml_file_path),
            "file_name": r2rml_file_path.name,
            "file_size": file_stat.st_size,
            "base_iri": base_iri,
            "schema_name": schema_name,
            "note": f"R2RML mapping read from tmp folder: {r2rml_file_path}",
            "usage_examples": [
                "Use with D2RQ Server: d2r-server r2rml_mapping.ttl",
                "Use with Ontop: ontop materialize -m r2rml_mapping.ttl",
                "Convert to RDF: r2rml r2rml_mapping.ttl > data.ttl",
            ],
        }

    except Exception as e:
        logger.error(f"Error downloading R2RML: {e}")
        return {
            "success": False,
            "error": f"Failed to download R2RML: {str(e)}",
            "error_type": "internal_error",
        }
