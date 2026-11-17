"""Security utilities for credential management and data protection."""

import base64
import json
import logging
import os
import re
import string
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from dotenv import load_dotenv

logger = logging.getLogger(__name__)


class SecurityLevel(Enum):
    """Security levels for different operations."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class SecureCredentialManager:
    """Secure credential management with encryption."""

    def __init__(self, master_password: str | None = None):
        """Initialize with optional master password for encryption."""
        self._cipher: Fernet | None = None
        self._salt_file = Path.home() / ".mcp_credential_salt"

        # Try to get master password from environment if not provided
        if not master_password:
            load_dotenv()
            master_password = os.getenv("MCP_MASTER_PASSWORD")

        if master_password:
            self._initialize_encryption(master_password)

    def _initialize_encryption(self, master_password: str) -> None:
        """Initialize encryption with master password."""
        # Get or create persistent salt
        salt = self._get_or_create_salt()

        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=100000,
        )
        key = base64.urlsafe_b64encode(kdf.derive(master_password.encode()))
        self._cipher = Fernet(key)
        self._salt = salt

    def _get_or_create_salt(self) -> bytes:
        """Get existing salt or create a new one with secure permissions."""
        try:
            if self._salt_file.exists():
                with open(self._salt_file, "rb") as f:
                    salt = f.read()
                    if len(salt) == 16:  # Validate salt length
                        logger.debug("Using existing salt for credential encryption")
                        return salt
                    else:
                        logger.warning("Invalid salt file, creating new salt")

            # Create new salt
            salt = os.urandom(16)

            # Write salt with secure permissions
            with open(self._salt_file, "wb") as f:
                f.write(salt)

            # Set restrictive permissions (owner read/write only)
            self._salt_file.chmod(0o600)

            logger.info("Created new salt file: %s", self._salt_file)
            return salt

        except OSError as e:
            logger.error("Failed to manage salt file: %s", e)
            # Fallback to session-only salt (less secure)
            logger.warning(
                "Using session-only salt - encrypted data will not persist "
                "across restarts"
            )
            return os.urandom(16)

    def encrypt_credentials(self, credentials: dict[str, Any]) -> str:
        """Encrypt credentials dictionary."""
        if not self._cipher:
            raise ValueError("Encryption not initialized")

        # Remove sensitive data from logs
        safe_creds = self._sanitize_credentials(credentials)
        logger.debug(
            "Encrypting credentials for %s connection",
            safe_creds.get("type", "unknown"),
        )

        json_data = json.dumps(credentials).encode()
        encrypted_data = self._cipher.encrypt(json_data)
        return base64.urlsafe_b64encode(encrypted_data).decode()

    def decrypt_credentials(self, encrypted_data: str) -> dict[str, Any]:
        """Decrypt credentials dictionary."""
        if not self._cipher:
            raise ValueError("Encryption not initialized")

        try:
            decoded_data = base64.urlsafe_b64decode(encrypted_data.encode())
            decrypted_data = self._cipher.decrypt(decoded_data)
            credentials_dict: dict[str, Any] = json.loads(decrypted_data.decode())
            return credentials_dict
        except Exception as e:
            logger.error("Failed to decrypt credentials: %s", e)
            raise ValueError("Invalid or corrupted credential data") from e

    def _sanitize_credentials(self, credentials: dict[str, Any]) -> dict[str, Any]:
        """Remove sensitive information for logging."""
        sensitive_keys = {
            "password",
            "passwd",
            "pwd",
            "secret",
            "token",
            "key",
            "pat",
            "api_key",
        }
        # Check if key contains any sensitive substrings
        return {
            k: (
                "***REDACTED***"
                if any(sens in k.lower() for sens in sensitive_keys) and v
                else v
            )
            for k, v in credentials.items()
        }


class SQLInjectionValidator:
    """Validates SQL queries to prevent injection attacks."""

    # Dangerous SQL patterns that should be blocked
    DANGEROUS_PATTERNS: ClassVar[list[str]] = [
        # Multiple statements with DDL/DML
        r";.*(?:DROP|DELETE|INSERT|UPDATE|CREATE|ALTER|TRUNCATE)",
        # Classic injection patterns (but NOT blocking UNION/UNION ALL)
        r"OR\s+1\s*=\s*1",  # Always true conditions
        r"AND\s+1\s*=\s*1",  # Always true conditions
        r"OR\s+'[^']*'\s*=\s*'[^']*'",  # String-based always true
        # SQL comment injection patterns
        r"'[^']*'\s*--",  # String literal followed by SQL comment (injection pattern)
        r"/\*.*\*/",  # Block comments (can be used to obfuscate or inject)
        # Multiple semicolons or semicolon with dangerous operations
        r";\s*;",  # Multiple semicolons
        r";\s*(?:DROP|DELETE|INSERT|UPDATE|CREATE|ALTER|TRUNCATE)",  # Semicolon followed by DDL/DML
        # SQL Server command execution
        r"xp_cmdshell|sp_executesql|exec\s*\(",
        # MySQL file operations
        r"load_file|into\s+outfile|into\s+dumpfile",
        # PostgreSQL file operations
        r"pg_read_file|copy.*from|copy.*to",
        # System tables that shouldn't be accessed directly in normal queries
        r"information_schema\.(?:user_privileges|schema_privileges|table_privileges)",
        r"pg_catalog\.pg_authid|pg_catalog\.pg_user_mapping",
        # MySQL's "mysql" schema is the server's own data, not a metadata
        # catalog: mysql.user holds account names and password hashes, and
        # mysql.db / mysql.tables_priv / mysql.procs_priv hold grants. These
        # were never blocked here, so they relied on the ontology check
        # incidentally rejecting them -- which is not a security boundary.
        r"mysql\.(?:user|db|global_priv|tables_priv|columns_priv|procs_priv|proxies_priv)",
        # Snowflake's grant and login history views carry the same class of
        # privilege metadata as the ANSI *_privileges views above.
        r"snowflake\.account_usage\.(?:grants_to_users|grants_to_roles|login_history|users)",
        # Dangerous UNION queries attempting to extract sensitive data
        r"UNION\s+(?:ALL\s+)?SELECT\s+.*(?:password|pwd|secret|token|key|admin)",
    ]

    # Safe SQL patterns that are allowed
    # Optional comment prefix: allows -- comments and /* */ comments at start of query
    # Note: After _clean_query(), newlines become spaces, so -- comment ends at next SQL keyword
    # Pattern matches: optional (-- followed by non-SQL text, or /* ... */) followed by whitespace
    _OPT_COMMENT = r"^(?:--[^;]*?\s+)?"

    SAFE_PATTERNS: ClassVar[list[str]] = [
        # Basic SELECT queries with JOINs (with optional leading comments)
        _OPT_COMMENT + r"SELECT\s+.+\s+FROM\s+.+$",
        # CTEs (WITH clauses) - now more permissive for complex queries
        _OPT_COMMENT + r"WITH\s+.+\s+SELECT\s+.+$",
        # Metadata queries
        _OPT_COMMENT + r'DESCRIBE\s+[\w\."]+$',
        _OPT_COMMENT + r'DESC\s+[\w\."]+$',
        _OPT_COMMENT
        + r'SHOW\s+(?:TABLES|COLUMNS|DATABASES|SCHEMAS)(?:\s+FROM\s+[\w\."]+)?$',
        _OPT_COMMENT + r"EXPLAIN\s+.+$",
        # UNION queries (both UNION and UNION ALL) - Note: Will still be blocked if dangerous patterns match
        r".+\s+UNION\s+(?:ALL\s+)?SELECT\s+.+",
    ]

    def __init__(self) -> None:
        self.compiled_dangerous = [
            re.compile(pattern, re.IGNORECASE | re.MULTILINE)
            for pattern in self.DANGEROUS_PATTERNS
        ]
        self.compiled_safe = [
            re.compile(pattern, re.IGNORECASE | re.MULTILINE)
            for pattern in self.SAFE_PATTERNS
        ]

    def validate_query(self, sql_query: str) -> dict[str, Any]:
        """Validate SQL query for security issues."""
        if not sql_query or not sql_query.strip():
            return {
                "is_safe": False,
                "risk_level": SecurityLevel.HIGH.value,
                "issues": ["Empty query"],
                "sanitized_query": "",
            }

        # Clean up query for analysis
        cleaned_query = self._clean_query(sql_query)
        issues = []
        risk_level = SecurityLevel.LOW

        # Check for dangerous patterns
        for pattern in self.compiled_dangerous:
            if pattern.search(cleaned_query):
                issues.append(
                    f"Potentially dangerous SQL pattern detected: " f"{pattern.pattern}"
                )
                risk_level = SecurityLevel.CRITICAL

        # Check for multiple statements. Blank out single-quoted string literals
        # first so a semicolon *inside* a literal (e.g. WHERE name = 'a;b') is not
        # miscounted as a statement separator. Authoritative multi-statement
        # detection is the parser-based analyze_sql_statement() gate; this is a
        # coarse first filter.
        literal_stripped = re.sub(r"'(?:[^']|'')*'", "''", cleaned_query)
        statements = [s.strip() for s in literal_stripped.split(";") if s.strip()]
        if len(statements) > 1:
            issues.append("Multiple SQL statements not allowed")
            risk_level = SecurityLevel.CRITICAL

        # Validate against safe patterns (if no dangerous patterns found)
        is_safe = len(issues) == 0
        if is_safe and statements:
            statement = statements[0]
            matches_safe_pattern = any(
                pattern.match(statement) for pattern in self.compiled_safe
            )
            if not matches_safe_pattern:
                issues.append("Query does not match approved safe patterns")
                risk_level = SecurityLevel.MEDIUM
                is_safe = False

        return {
            "is_safe": is_safe,
            "risk_level": risk_level.value,
            "issues": issues,
            "sanitized_query": self._sanitize_query_for_logging(cleaned_query),
        }

    def _clean_query(self, sql_query: str) -> str:
        """Clean and normalize SQL query for analysis."""
        # Remove leading/trailing whitespace
        cleaned = sql_query.strip()

        # Normalize whitespace
        cleaned = re.sub(r"\s+", " ", cleaned)

        return cleaned

    def _sanitize_query_for_logging(self, sql_query: str, max_length: int = 200) -> str:
        """Sanitize SQL query for safe logging."""
        # Remove potential sensitive data patterns
        sanitized = re.sub(r"'[^']*'", "'***'", sql_query)  # String literals
        # Quoted identifiers with potential data
        sanitized = re.sub(r'"[^"]*"', '"***"', sanitized)

        # Truncate if too long
        if len(sanitized) > max_length:
            sanitized = sanitized[:max_length] + "..."

        return sanitized


class IdentifierValidator:
    """Validates database identifiers to prevent injection."""

    # Valid identifier pattern (letters, numbers, underscores only - no hyphens)
    VALID_IDENTIFIER = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

    @classmethod
    def validate_identifier(cls, identifier: str) -> bool:
        """Validate a single database identifier."""
        if not identifier:
            return False

        # Check length (reasonable limit)
        if len(identifier) > 128:
            return False

        return bool(cls.VALID_IDENTIFIER.match(identifier))

    @classmethod
    def validate_qualified_identifier(cls, identifier: str) -> bool:
        """Validate schema.table or database.schema.table identifier."""
        if not identifier:
            return False

        parts = identifier.split(".")
        if len(parts) > 3:  # database.schema.table max
            return False

        return all(cls.validate_identifier(part) for part in parts)

    @classmethod
    def sanitize_identifier(cls, identifier: str) -> str:
        """Sanitize identifier for safe usage."""
        if not identifier:
            return ""

        # Remove invalid characters (including hyphens)
        sanitized = re.sub(r"[^a-zA-Z0-9_]", "_", identifier)

        # Ensure starts with letter or underscore
        if sanitized and not sanitized[0].isalpha() and sanitized[0] != "_":
            sanitized = "_" + sanitized

        # Limit length
        return sanitized[:128]


def audit_log_security_event(
    event_type: str,
    details: dict[str, Any],
    risk_level: SecurityLevel = SecurityLevel.MEDIUM,
) -> None:
    """Log security-related events for auditing."""
    # Sanitize details before logging
    safe_details = {}
    sensitive_keys = {
        "password",
        "passwd",
        "pwd",
        "secret",
        "token",
        "key",
        "pat",
        "api_key",
    }

    for key, value in details.items():
        # Check if key contains any sensitive substring
        if any(sens in key.lower() for sens in sensitive_keys):
            safe_details[key] = "***REDACTED***"
        else:
            safe_details[key] = value

    # Use f-string formatting to actually substitute values
    logger.warning(
        f"SECURITY_AUDIT: {event_type} | Risk: {risk_level.value} | Details: {safe_details}"
    )


#: AST node types that mutate data or schema, or lock rows -- never allowed for
#: analytics SQL. ``Into`` is ``SELECT ... INTO new_table``, which PostgreSQL
#: runs as CREATE TABLE AS; ``Lock`` is ``FOR UPDATE`` / ``FOR SHARE``.
_WRITE_EXPRESSIONS = (
    "Insert",
    "Update",
    "Delete",
    "Merge",
    "Create",
    "Drop",
    "Alter",
    "TruncateTable",
    "Into",
    "Lock",
)

#: Functions no analytics query may call, whatever the dialect. A plain SELECT
#: can still change state, hold the server or reach past the database through
#: a function call, and neither the statement type nor OBQC notices: OBQC
#: checks tables and columns, and a table function such as ``read_csv(...)``
#: has no table name to check. Lower case.
_FORBIDDEN_FUNCTIONS = frozenset(
    {
        # Change state a rollback does not undo, or the session itself.
        "nextval",
        "setval",
        "getnextval",
        "set_config",
        "pg_notify",
        "get_lock",
        "release_lock",
        "release_all_locks",
        # Act on other sessions or on the server.
        "pg_terminate_backend",
        "pg_cancel_backend",
        "pg_reload_conf",
        "pg_rotate_logfile",
        "pg_promote",
        "pg_start_backup",
        "pg_stop_backup",
        # Hold a connection for as long as the caller likes.
        "pg_sleep",
        "pg_sleep_for",
        "pg_sleep_until",
        "sleep",
        "sleepeachrow",
        "benchmark",
        "master_pos_wait",
        "source_pos_wait",
        "system$wait",
        # Read files on the database host.
        "pg_read_file",
        "pg_read_binary_file",
        "pg_stat_file",
        "load_file",
        "loread",
        "lowrite",
        "glob",
        "sniff_csv",
        "file",
        "executable",
        # Reach other databases, object stores or the network.
        "url",
        "urlcluster",
        "s3",
        "s3cluster",
        "gcs",
        "hdfs",
        "hdfscluster",
        "azureblobstorage",
        "azureblobstoragecluster",
        "deltalake",
        "iceberg",
        "hudi",
        "remote",
        "remotesecure",
        "mysql",
        "postgresql",
        "mongodb",
        "redis",
        "sqlite",
        "jdbc",
        "odbc",
        "external_query",
        "http_request",
        # Run SQL passed as a string, past every check in this module.
        "query",
        "query_table",
        "query_to_xml",
        "query_to_xmlschema",
        "query_to_xml_and_xmlschema",
        "cursor_to_xml",
        "cursor_to_xmlschema",
    }
)

#: Families of the same, matched by prefix.
_FORBIDDEN_FUNCTION_PREFIXES = (
    "pg_advisory_",  # session-level locks
    "pg_try_advisory_",
    "pg_ls_",  # list directories on the database host
    "lo_",  # large objects: lo_import and lo_export touch host files
    "dblink",  # SQL on another database, committed there
    "pg_create_",  # replication slots, restore points
    "pg_drop_",
    "pg_replication_",
    "pg_logical_",
    "pg_switch_",
    "pg_backup_",
    "pg_wal_replay_",
    "pg_stat_reset",
    "read_",  # DuckDB read_csv/read_parquet/read_text, Databricks read_files
    "parquet_",
    "iceberg_",
    "delta_",
    "postgres_",  # DuckDB scanners for other databases
    "mysql_",
    "sqlite_",
    "table_to_xml",  # whole tables as XML, past OBQC's table check
    "schema_to_xml",
    "database_to_xml",
    "system$cancel",  # Snowflake: stop other queries and sessions
    "system$abort",
)

#: Characters that make part of a DuckDB table reference a file path or URL.
#: DuckDB reads a name it cannot find in its catalog as a file, joining the
#: parts with dots (``FROM "data"."csv"`` reads data.csv), and a real table,
#: schema or catalog name almost never contains one of these. A plain dotted
#: name (``FROM data.csv``) looks like schema.table, so the DuckDB driver also
#: checks every reference against its catalog before the query runs.
_DUCKDB_FILE_NAME_CHARS = frozenset("/\\:.")

#: Where the explained statement starts, after EXPLAIN and its options.
_EXPLAIN_BODY = re.compile(
    r"\b(?:SELECT|WITH|VALUES|TABLE|FROM|INSERT|UPDATE|DELETE|MERGE|UPSERT|"
    r"REPLACE|CREATE|DROP|ALTER|TRUNCATE|COPY|CALL|EXEC|EXECUTE)\b",
    re.IGNORECASE,
)

#: The EXPLAIN option that runs the statement instead of only planning it.
_EXPLAIN_ANALYZE = re.compile(r"\bANALY[SZ]E\b", re.IGNORECASE)

_EXPLAIN_ANALYZE_ERROR = (
    "EXPLAIN ANALYZE runs the statement it explains; use EXPLAIN without ANALYZE"
)


def _is_forbidden_function(name: str) -> bool:
    """Whether a function of this name may not appear in an analytics query."""
    name = name.lower()
    return name in _FORBIDDEN_FUNCTIONS or name.startswith(_FORBIDDEN_FUNCTION_PREFIXES)


def explain_runs_statement(sql_query: str) -> bool:
    """Whether *sql_query* is an EXPLAIN that executes the statement it explains.

    ``EXPLAIN ANALYZE`` (PostgreSQL, MySQL, DuckDB, Trino) runs the statement to
    time it, so ``EXPLAIN ANALYZE DELETE ...`` deletes. Only the options before
    the explained statement are read, so a column named ``analyze`` is not
    mistaken for one.

    Args:
        sql_query: SQL text with any leading comments already removed.

    Returns:
        True for an EXPLAIN carrying the ANALYZE option, False otherwise.
    """
    text = sql_query.strip()
    if not re.match(r"EXPLAIN\b", text, re.IGNORECASE):
        return False
    options = text[len("EXPLAIN") :]
    body = _EXPLAIN_BODY.search(options)
    if body is not None:
        options = options[: body.start()]
    return _EXPLAIN_ANALYZE.search(options) is not None


_ASCII_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)
_ASCII_UPPER = str.maketrans(string.ascii_lowercase, string.ascii_uppercase)


def ascii_lower(name: str) -> str:
    """*name* with ASCII letters lower-cased and every other character kept.

    DuckDB compares identifiers this way: ``DÀTA`` and ``dÀta`` are one name,
    ``dàta`` is another. Python's ``str.lower`` folds the ``À`` too, which
    would call two different DuckDB names the same.
    """
    return name.translate(_ASCII_LOWER)


def _snowflake_identifier(identifier: Any) -> str:
    """An identifier as Snowflake resolves it: unquoted upper-cased, quoted as is."""
    from sqlglot import expressions as exp

    if isinstance(identifier, exp.Identifier):
        text = str(identifier.this)
        return text if identifier.quoted else text.translate(_ASCII_UPPER)
    return str(identifier or "").translate(_ASCII_UPPER)


def _binds_snowflake_column(column: Any, item: Any) -> bool:
    """Whether the FROM/JOIN item *item* is what *column*'s qualifier names.

    An aliased item -- table, derived table, LATERAL, table function --
    answers only to its alias, and only for a one-part qualifier. An
    unaliased table answers to its name alone, or to its name with exactly the
    qualifiers it was written with. Anything less certain --
    ``otherdb.sales.seq`` against ``FROM sales.seq`` -- is not a match.
    """
    from sqlglot import expressions as exp

    qualifier = [
        _snowflake_identifier(column.args[key])
        for key in ("catalog", "db", "table")
        if column.args.get(key)
    ]
    alias = item.args.get("alias")
    if alias is not None and alias.args.get("this") is not None:
        return qualifier == [_snowflake_identifier(alias.this)]
    if not isinstance(item, exp.Table):
        return False
    written = [
        _snowflake_identifier(item.args[key])
        for key in ("catalog", "db", "this")
        if item.args.get(key)
    ]
    return qualifier == written or qualifier == written[-1:]


def _contains(ancestor: Any, node: Any) -> bool:
    """Whether *node* is *ancestor* or lies inside it."""
    while node is not None:
        if node is ancestor:
            return True
        node = node.parent
    return False


def _items_visible_at(select: Any, position: Any) -> list[Any]:
    """The FROM/JOIN items of *select* that *position*, inside it, can name.

    The select list, WHERE and the clauses after it see every item. A join's
    ON condition sees the items joined so far. An item -- a LATERAL subquery
    or a table function's arguments -- sees only the items before it.
    """
    from_clause = select.args.get("from_") or select.args.get("from")
    joins = select.args.get("joins") or []
    items = ([from_clause.this] if from_clause is not None else []) + [
        join.this for join in joins
    ]
    for index, join in enumerate(joins):
        condition = join.args.get("on")
        if condition is not None and _contains(condition, position):
            return items[: index + 2]
    for index, item in enumerate(items):
        if _contains(item, position):
            return items[:index]
    return items


def _enclosing_position(select: Any) -> Any:
    """Where *select* stands in the query around it, if it sees that query's items.

    An expression subquery (in the select list, WHERE, ON, IN, EXISTS ...)
    and a LATERAL subquery see their enclosing query; a derived table in
    FROM, a CTE body and the root query do not. Returns the node to look up
    visibility from in the enclosing query, or None.
    """
    from sqlglot import expressions as exp

    node = select
    while isinstance(node.parent, exp.SetOperation):
        node = node.parent  # a UNION branch stands where the UNION does
    parent = node.parent
    if parent is None or isinstance(parent, (exp.CTE, exp.From, exp.Join)):
        return None
    if isinstance(parent, exp.Subquery):
        holder = parent.parent
        if isinstance(holder, exp.Lateral):
            return holder
        if holder is None or isinstance(holder, (exp.From, exp.Join, exp.CTE)):
            return None
        return parent
    return node


def _snowflake_column_is_bound(column: Any) -> bool:
    """Whether *column*'s qualifier names a FROM/JOIN item visible where it stands."""
    from sqlglot import expressions as exp

    position = column
    while position is not None:
        select = position.find_ancestor(exp.Select)
        if select is None:
            return False
        if any(
            _binds_snowflake_column(column, item)
            for item in _items_visible_at(select, position)
        ):
            return True
        position = _enclosing_position(select)
    return False


def _snowflake_sequence_references(stmt: Any) -> list[str]:
    """``seq.NEXTVAL`` references in a Snowflake statement, which advance seq.

    Snowflake reads ``x.NEXTVAL`` as a column when ``x`` names a FROM or JOIN
    item visible where it is written, and as the sequence ``x`` otherwise.
    Visibility follows the query's structure rather than sqlglot's scopes,
    which file a LATERAL subquery's columns under the enclosing query and so
    would let an item *after* the LATERAL bind them: see
    :func:`_items_visible_at` and :func:`_enclosing_position`. A CTE binds
    nothing unless selected from. Identifiers compare as Snowflake resolves
    them (see :func:`_snowflake_identifier`).
    """
    from sqlglot import expressions as exp

    return list(
        dict.fromkeys(
            f"sequence {column.sql(dialect='snowflake')}"
            for column in stmt.find_all(exp.Column)
            if column.table
            and column.name.upper() == "NEXTVAL"
            and not _snowflake_column_is_bound(column)
        )
    )


def _unsafe_operations(stmt: Any, dialect: str) -> list[str]:
    """Function calls and file references *stmt* makes that a read may not."""
    from sqlglot import expressions as exp

    found = []
    for func in stmt.find_all(exp.Func):
        name = func.name if isinstance(func, exp.Anonymous) else func.sql_name()
        if name and _is_forbidden_function(name):
            found.append(f"function {name.lower()}()")
    if dialect == "snowflake":
        found.extend(_snowflake_sequence_references(stmt))
    if dialect == "duckdb":
        for table in stmt.find_all(exp.Table):
            parts = [part for part in (table.catalog, table.db, table.name) if part]
            if table.name and any(_DUCKDB_FILE_NAME_CHARS & set(p) for p in parts):
                found.append(f"file reference '{'.'.join(parts)}'")
    return list(dict.fromkeys(found))


def table_references(sql_query: str, dialect: str) -> list[tuple[str, ...]] | None:
    """The tables and views *sql_query* names, each as its name parts.

    A reference keeps the parts written -- ``sales.orders`` is
    ``("sales", "orders")`` -- folded with :func:`ascii_lower`, the way DuckDB
    compares names, and leaves out CTE names and table functions.

    Args:
        sql_query: One SQL statement.
        dialect: sqlglot dialect name.

    Returns:
        The references, or None when the statement does not parse.
    """
    import sqlglot
    from sqlglot import expressions as exp

    try:
        statement = sqlglot.parse_one(sql_query, dialect=dialect)
    except Exception as e:  # sqlglot.errors.ParseError and friends
        logger.debug(f"table_references: statement does not parse: {e}")
        return None
    ctes = {ascii_lower(cte.alias_or_name) for cte in statement.find_all(exp.CTE)}
    references = []
    for table in statement.find_all(exp.Table):
        if not table.name:
            continue  # a table function: read_csv(...), range(10)
        parts = tuple(
            ascii_lower(part) for part in (table.catalog, table.db, table.name) if part
        )
        if len(parts) == 1 and parts[0] in ctes:
            continue
        references.append(parts)
    return list(dict.fromkeys(references))


def _explain_problem(stmt: Any, dialect: str) -> dict[str, Any] | None:
    """Why an EXPLAIN that sqlglot kept as a raw command may not run, if it may not.

    sqlglot parses EXPLAIN as an opaque ``Command`` in most dialects, so the
    explained statement is cut out of the text and analysed on its own: it has
    to be a read the gate would allow by itself.
    """
    explained = stmt.expression.name if stmt.expression is not None else ""
    text = f"EXPLAIN {explained}"
    if explain_runs_statement(text):
        return {
            "query_type": "UNSAFE",
            "unsafe_operations": ["EXPLAIN ANALYZE"],
            "error": _EXPLAIN_ANALYZE_ERROR,
        }
    body = _EXPLAIN_BODY.search(text, len("EXPLAIN"))
    if body is None:
        return {
            "query_type": "UNKNOWN",
            "error": "EXPLAIN must be followed by a SELECT query",
        }
    inner = analyze_sql_statement(text[body.start() :], dialect=dialect)
    if inner["query_type"] in ("SELECT", "CTE_SELECT"):
        return None
    return {
        "query_type": (
            inner["query_type"] if inner["query_type"] != "METADATA" else "UNKNOWN"
        ),
        "write_operations": inner["write_operations"],
        "unsafe_operations": inner["unsafe_operations"],
        "error": f"EXPLAIN of a statement that may not run: "
        f"{inner['error'] or 'not a SELECT query'}",
    }


def analyze_sql_statement(sql_query: str, dialect: str = "postgres") -> dict[str, Any]:
    """Dialect-aware structural analysis of a SQL statement using sqlglot.

    This is the *primary*, parser-based safety gate. Unlike regex/``startswith``
    checks it is not fooled by semicolons inside string literals (false
    "multiple statements") or by write operations buried after a CTE
    (``WITH x AS (...) INSERT ...`` — which a ``startswith('WITH')`` check would
    wrongly allow). The regex :class:`SQLInjectionValidator` remains a first
    filter; this is the authoritative read-only / single-statement decision.

    A statement only counts as a read if it also calls none of the functions
    that change state, hold the server or reach outside the database
    (``pg_sleep``, ``setval``, ``read_csv``, ``dblink`` ...), takes no row locks,
    reads no file by name (DuckDB's ``FROM 'data.csv'``), and -- for EXPLAIN --
    explains such a read without running it (no ``ANALYZE``).

    Args:
        sql_query: The SQL statement to analyze.
        dialect: sqlglot dialect name (e.g. ``postgres``, ``snowflake``).

    Returns:
        Dict with: ``parsed`` (bool), ``single_statement`` (bool),
        ``query_type`` (SELECT|CTE_SELECT|METADATA|WRITE|UNSAFE|UNKNOWN),
        ``is_read_only`` (bool), ``write_operations`` (list[str]),
        ``unsafe_operations`` (list[str]), ``affected_tables`` (list[str]),
        ``error`` (str|None).
    """
    import sqlglot
    from sqlglot import expressions as exp

    result: dict[str, Any] = {
        "parsed": False,
        "single_statement": True,
        "query_type": "UNKNOWN",
        "is_read_only": False,
        "write_operations": [],
        "unsafe_operations": [],
        "affected_tables": [],
        "error": None,
    }

    try:
        statements = [
            s for s in sqlglot.parse(sql_query, dialect=dialect) if s is not None
        ]
    except Exception as e:  # sqlglot.errors.ParseError and friends
        result["error"] = f"SQL parse error: {e}"
        return result

    result["parsed"] = True
    if not statements:
        result["error"] = "Empty query"
        return result
    if len(statements) > 1:
        result["single_statement"] = False
        result["error"] = "Multiple SQL statements are not allowed"
        return result

    stmt = statements[0]
    write_types = tuple(
        getattr(exp, name) for name in _WRITE_EXPRESSIONS if hasattr(exp, name)
    )

    # Detect write/DDL nodes anywhere in the tree (including nested in CTEs).
    writes = sorted({type(n).__name__ for n in stmt.find_all(*write_types)})
    if writes:
        result["query_type"] = "WRITE"
        result["write_operations"] = writes
        result["error"] = f"Write/DDL operations are not allowed: {', '.join(writes)}"
        return result

    unsafe = _unsafe_operations(stmt, dialect)
    if unsafe:
        result["query_type"] = "UNSAFE"
        result["unsafe_operations"] = unsafe
        result["error"] = f"Not allowed in a read query: {', '.join(unsafe)}"
        return result

    if isinstance(stmt, (exp.Select, exp.Union)):
        result["query_type"] = (
            "CTE_SELECT" if stmt.find(exp.With) is not None else "SELECT"
        )
        result["is_read_only"] = True
    elif isinstance(stmt, exp.Describe) and (
        str(stmt.args.get("style") or "").upper() == "ANALYZE"
    ):
        # MySQL's EXPLAIN parses to Describe; its body was walked above.
        result["query_type"] = "UNSAFE"
        result["unsafe_operations"] = ["EXPLAIN ANALYZE"]
        result["error"] = _EXPLAIN_ANALYZE_ERROR
        return result
    elif isinstance(stmt, (exp.Describe, exp.Show)):
        result["query_type"] = "METADATA"
        result["is_read_only"] = True
    elif isinstance(stmt, exp.Command):
        # Anything sqlglot cannot parse becomes a Command, so only the metadata
        # keywords pass, and EXPLAIN only for a read it does not run.
        keyword = str(stmt.this or "").upper()
        if keyword == "EXPLAIN":
            problem = _explain_problem(stmt, dialect)
            if problem is not None:
                result.update(problem)
                return result
        elif keyword not in ("SHOW", "DESCRIBE", "DESC"):
            result["error"] = "Only SELECT, CTE, and metadata queries are allowed"
            return result
        result["query_type"] = "METADATA"
        result["is_read_only"] = True
    else:
        result["error"] = "Only SELECT, CTE, and metadata queries are allowed"
        return result

    result["affected_tables"] = sorted(
        {t.name for t in stmt.find_all(exp.Table) if t.name}
    )
    return result


# Global instances
sql_validator = SQLInjectionValidator()
identifier_validator = IdentifierValidator()
