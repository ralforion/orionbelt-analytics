"""R2RML mapping generator for creating RDB to RDF mappings from database schemas.

This module generates R2RML (RDB to RDF Mapping Language) mappings that define
how relational database tables map to RDF triples. The generated mappings follow
the W3C R2RML specification: https://www.w3.org/TR/r2rml/

R2RML mappings enable:
- Converting relational data to RDF format
- Semantic integration of database content
- SPARQL querying over relational databases
- Linked Data publication from databases

The mapping uses the same vocabulary as the generated OWL ontology (see
``ontology_naming``), so a virtual knowledge graph engine such as Ontop can
serve the database through the mapping and the ontology together.
"""

import logging

from .constants import DEFAULT_BASE_URI
from .database_manager import TableInfo
from .ontology_naming import (
    ForeignKey,
    class_local_name,
    column_property_local_name,
    foreign_keys,
)

logger = logging.getLogger(__name__)


def _sql_identifier(name: str) -> str:
    """Delimited SQL identifier, so case and special characters are kept."""
    return '"' + name.replace('"', '""') + '"'


def _turtle_string(value: str) -> str:
    """Turtle string literal for ``value``."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _template_text(text: str) -> str:
    """Escape text that appears literally in an R2RML string template."""
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


class R2RMLGenerator:
    """Generates R2RML mappings from database schema information.

    R2RML (RDB to RDF Mapping Language) is a W3C standard for expressing
    customized mappings from relational databases to RDF datasets.

    This generator creates TriplesMap definitions that include:
    - Subject maps with IRI templates based on primary keys
    - Predicate-object maps for column values
    - Referencing object maps (join conditions) for foreign key relationships

    Classes and properties are named in the ontology vocabulary; only the
    resources for individual rows live under ``base_iri``.
    """

    def __init__(
        self,
        base_iri: str = "http://example.com/",
        database_name: str = "database",
        vocabulary_iri: str = DEFAULT_BASE_URI,
    ):
        """Initialize the R2RML generator.

        Args:
            base_iri: Base IRI for generated RDF resources (must end with /)
            database_name: Name of the database (used in comments)
            vocabulary_iri: Base URI of the ontology whose classes and
                properties the mapping uses
        """
        self.base_iri = base_iri if base_iri.endswith("/") else base_iri + "/"
        self.database_name = database_name
        self.vocabulary_iri = (
            vocabulary_iri if vocabulary_iri.endswith("/") else vocabulary_iri + "/"
        )

    def generate_from_schema(
        self, tables_info: list[TableInfo], schema_name: str | None = None
    ) -> str:
        """Generate R2RML mapping from database schema information.

        Args:
            tables_info: List of TableInfo objects containing schema metadata
            schema_name: Optional schema name to include in table references

        Returns:
            R2RML mapping in Turtle format
        """
        if not tables_info:
            logger.warning("No tables provided for R2RML generation")
            return ""

        lines = []

        # Add prefixes
        lines.extend(self._generate_prefixes())
        lines.append("")

        # Build a lookup for tables by name for FK resolution
        table_lookup = {t.name: t for t in tables_info}

        # Generate TriplesMap for each table
        for table_info in tables_info:
            lines.extend(
                self._generate_triples_map(table_info, schema_name, table_lookup)
            )
            lines.append("")

        return "\n".join(lines)

    def _generate_prefixes(self) -> list[str]:
        """Generate R2RML prefix declarations."""
        prefixes = [
            "@prefix rr: <http://www.w3.org/ns/r2rml#> .",
            f"@prefix ont: <{self.vocabulary_iri}> .",
        ]
        return prefixes

    @staticmethod
    def _map_name(table_name: str) -> str:
        """Relative IRI of the TriplesMap for a table."""
        return f"<#{class_local_name(table_name)}Mapping>"

    def _generate_triples_map(
        self,
        table_info: TableInfo,
        schema_name: str | None,
        table_lookup: dict[str, TableInfo],
    ) -> list[str]:
        """Generate a TriplesMap for a single table.

        Args:
            table_info: TableInfo object for the table
            schema_name: Optional schema name
            table_lookup: Dictionary mapping table names to TableInfo objects

        Returns:
            List of Turtle lines for the TriplesMap
        """
        lines = []

        table_name = table_info.name
        effective_schema = schema_name or table_info.schema

        # Delimited identifiers keep the catalog's exact case, which matters
        # on case-sensitive dialects such as Snowflake.
        if effective_schema:
            full_table_ref = (
                f"{_sql_identifier(effective_schema)}.{_sql_identifier(table_name)}"
            )
        else:
            full_table_ref = _sql_identifier(table_name)

        lines.append(self._map_name(table_name))
        lines.append("    a rr:TriplesMap ;")
        lines.append(
            f"    rr:logicalTable [ rr:tableName {_turtle_string(full_table_ref)} ] ;"
        )

        # Subject map
        lines.append("    rr:subjectMap [")
        lines.extend(self._generate_subject_map(table_info))
        lines.append(f"        rr:class ont:{class_local_name(table_name)}")
        lines.append("    ] ;")

        # Every column is a datatype property, foreign keys included: the
        # ontology declares one for each column next to the object property.
        for column in table_info.columns:
            predicate = column_property_local_name(table_name, column.name)
            column_ref = _turtle_string(_sql_identifier(column.name))
            lines.append(
                f"    rr:predicateObjectMap [ rr:predicate ont:{predicate} ; "
                f"rr:objectMap [ rr:column {column_ref} ] ] ;"
            )

        for key, fk_columns in self._resolve_foreign_keys(table_info, table_lookup):
            lines.extend(self._generate_fk_predicate_object_map(key, fk_columns))

        # Remove trailing semicolon from last predicate-object map and add period
        if lines and lines[-1].endswith(" ;"):
            lines[-1] = lines[-1][:-2] + " ."

        return lines

    def _generate_subject_map(self, table_info: TableInfo) -> list[str]:
        """Generate the term lines of a table's subject map.

        A table with a primary key gets an IRI per row. Without one there is
        no column that identifies a row, so the row becomes a blank node keyed
        on its non-null columns rather than an IRI that may merge rows.

        Args:
            table_info: TableInfo object

        Returns:
            Turtle lines for the subject map's term
        """
        pk_columns = table_info.primary_keys
        if pk_columns:
            key_columns = pk_columns
        else:
            key_columns = [c.name for c in table_info.columns if not c.is_nullable]
            if not key_columns:
                key_columns = [c.name for c in table_info.columns]
            logger.warning(
                f"Table '{table_info.name}' has no primary key; "
                "mapping its rows to blank nodes"
            )

        # R2RML percent-encodes "/" inside a column value, so "/" separates
        # the parts of a composite key unambiguously.
        placeholders = "/".join(
            "{" + _template_text(_sql_identifier(c)) + "}" for c in key_columns
        )
        if pk_columns:
            prefix = _template_text(
                f"{self.base_iri}{class_local_name(table_info.name)}/"
            )
            return [f"        rr:template {_turtle_string(prefix + placeholders)} ;"]
        return [
            f"        rr:template {_turtle_string(placeholders)} ;",
            "        rr:termType rr:BlankNode ;",
        ]

    @staticmethod
    def _resolve_foreign_keys(
        table_info: TableInfo, table_lookup: dict[str, TableInfo]
    ) -> list[tuple[ForeignKey, list[tuple[str, str]]]]:
        """The table's foreign keys that can be mapped as joins.

        Keys are grouped and named as in the ontology (``foreign_keys``).

        Args:
            table_info: TableInfo object of the referencing table
            table_lookup: Dict mapping table names to TableInfo

        Returns:
            Each mappable key with its (child column, parent column) pairs
        """
        resolved = []
        for key in foreign_keys(table_info.name, table_info.foreign_keys):
            parent = table_lookup.get(key.referenced_table)
            if parent is None:
                # Mapping a join needs the parent's TriplesMap, which only
                # exists for tables of this mapping.
                logger.debug(
                    f"FK {table_info.name} -> {key.referenced_table}: parent "
                    "table not in mapping, keeping the column value only"
                )
                continue
            pairs = []
            for child, parent_column in key.column_pairs:
                if not parent_column and len(parent.primary_keys) == 1:
                    parent_column = parent.primary_keys[0]
                if parent_column:
                    pairs.append((child, parent_column))
            if pairs:
                resolved.append((key, pairs))
        return resolved

    def _generate_fk_predicate_object_map(
        self, key: ForeignKey, fk_columns: list[tuple[str, str]]
    ) -> list[str]:
        """Generate the object property map for one foreign key.

        A referencing object map joins to the parent table's TriplesMap, so
        the object is exactly the parent row's subject, whatever its key.

        Args:
            key: The foreign key
            fk_columns: (child column, parent column) pairs of the key

        Returns:
            List of Turtle lines for the predicate-object map
        """
        lines = [
            "    rr:predicateObjectMap [",
            f"        rr:predicate ont:{key.local_name} ;",
            "        rr:objectMap [",
            f"            rr:parentTriplesMap {self._map_name(key.referenced_table)} ;",
        ]
        for i, (child, parent) in enumerate(fk_columns):
            end = " ;" if i < len(fk_columns) - 1 else ""
            lines.append(
                "            rr:joinCondition [ "
                f"rr:child {_turtle_string(_sql_identifier(child))} ; "
                f"rr:parent {_turtle_string(_sql_identifier(parent))} ]{end}"
            )
        lines.append("        ]")
        lines.append("    ] ;")
        return lines
