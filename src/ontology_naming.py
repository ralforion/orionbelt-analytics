"""Local names of the OWL vocabulary generated from a database schema.

The ontology generator and the R2RML generator both name classes and
properties after tables and columns. They must agree exactly: a mapping that
emits ``ont:orders`` is only useful next to an ontology that declares
``ont:orders``. Both take their names from here.
"""

import re


def clean_name(name: str) -> str:
    """Clean a name to make it suitable for URIs."""
    if not name:
        return "unnamed"

    # Replace spaces and special characters with underscores
    cleaned = re.sub(r"[^a-zA-Z0-9_]", "_", name)

    # Ensure it starts with a letter or underscore
    if cleaned and not (cleaned[0].isalpha() or cleaned[0] == "_"):
        cleaned = "_" + cleaned

    return cleaned or "unnamed"


def class_local_name(table_name: str) -> str:
    """Local name of the OWL class for a table."""
    return clean_name(table_name)


def column_property_local_name(table_name: str, column_name: str) -> str:
    """Local name of the datatype property for a column."""
    return f"{clean_name(table_name)}_{clean_name(column_name)}"


def relationship_local_name(table_name: str, referenced_table: str) -> str:
    """Local name of the object property for a foreign key."""
    return f"{clean_name(table_name)}_has_{clean_name(referenced_table)}"
