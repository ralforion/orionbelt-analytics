"""
Schema Embedder - Generates vector embeddings for schema elements

Uses a lightweight embedding model to create semantic representations of:
- Tables (name, description, columns)
- Columns (name, type, relationships)
- Relationships (foreign keys, join paths)

Two backends are available:

``minilm`` (default)
    Sentence embeddings from all-MiniLM-L6-v2, run through the ONNX runtime
    that ships with ChromaDB. Places semantically related text near each other,
    so "which products are most profitable" reaches ``salesamount`` even though
    they share no words.

``tfidf``
    Bag-of-words fallback. It has no notion of synonymy: a query term absent
    from the fitted vocabulary contributes nothing, and a query whose terms are
    *all* absent produces a zero vector, which scores 0.0 against every element
    and degenerates the ranking to index order. It is kept only so the server
    still works with no model available (offline install, restricted network).
"""

import hashlib
import logging
import os
from dataclasses import dataclass
from typing import Any

import numpy as np

from .identity import qualified, quote_part

logger = logging.getLogger(__name__)

# Backend identifiers. MINILM is the default; TFIDF is the offline fallback.
MODEL_MINILM = "minilm"
MODEL_TFIDF = "tfidf"
MODEL_SENTENCE_TRANSFORMERS = "sentence-transformers"

DEFAULT_EMBEDDING_MODEL = MODEL_MINILM

# Bumped when a change to the embedding text, the backend, or the identity an
# element is stored under makes a persisted index incompatible with a fresh
# one. Both backends emit 384 dimensions, so a stale index loads without any
# shape error and silently returns nonsense -- the fingerprint is what makes
# that detectable, and a mismatch rebuilds the collection from the schema.
# 3: element ids carry the schema, so two schemas can hold the same table.
EMBEDDING_SCHEMA_VERSION = 3

# Texts submitted to a backend per inference call. Batching is what makes
# indexing bearable -- 256 column texts through MiniLM cost 16.3 s one at a
# time and 2.5 s in one batch, for bit-identical vectors -- but a whole schema
# in a single call would hold every intermediate tensor in memory at once.
EMBEDDING_BATCH_SIZE = 256


def vocabulary_fingerprint(vocabulary: dict[str, int]) -> str:
    """Identify a fitted vocabulary, so two vector spaces are never mixed.

    Args:
        vocabulary: Term to column index, as scikit-learn fits it.

    Returns:
        A short digest of the vocabulary, stable across processes.
    """
    joined = "\u0000".join(
        f"{term}:{index}" for term, index in sorted(vocabulary.items())
    )
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def resolve_embedding_model(requested: str | None = None) -> str:
    """Pick the embedding backend, honouring GRAPHRAG_EMBEDDING_MODEL.

    Args:
        requested: Explicit backend name, or None to read the environment.

    Returns:
        One of ``minilm``, ``tfidf`` or ``sentence-transformers``. Unknown
        names fall back to the default with a warning rather than raising, so a
        typo in .env cannot stop the server from starting.
    """
    name = (requested or os.getenv("GRAPHRAG_EMBEDDING_MODEL") or "").strip().lower()
    if not name:
        return DEFAULT_EMBEDDING_MODEL
    if name in (MODEL_MINILM, MODEL_TFIDF, MODEL_SENTENCE_TRANSFORMERS):
        return name
    logger.warning(
        f"Unknown GRAPHRAG_EMBEDDING_MODEL '{name}'; "
        f"using '{DEFAULT_EMBEDDING_MODEL}'. "
        f"Valid values: {MODEL_MINILM}, {MODEL_TFIDF}, "
        f"{MODEL_SENTENCE_TRANSFORMERS}."
    )
    return DEFAULT_EMBEDDING_MODEL


@dataclass
class SchemaElement:
    """Represents a schema element with its embedding."""

    element_type: str  # "table", "column", "relationship"
    element_id: str
    name: str
    description: str
    metadata: dict[str, Any]
    embedding: np.ndarray | None = None


class SchemaEmbedder:
    """Generates embeddings for schema elements using simple TF-IDF or sentence embeddings."""

    def __init__(self, embedding_model: str | None = None):
        """
        Initialize the schema embedder.

        Args:
            embedding_model: Backend name ("minilm", "tfidf",
                "sentence-transformers"), or None to resolve from
                GRAPHRAG_EMBEDDING_MODEL and fall back to the default.
        """
        self.embedding_model = resolve_embedding_model(embedding_model)
        self._initialize_model()

    def _initialize_model(self) -> None:
        """Initialize the embedding model.

        Any backend that cannot be loaded degrades to TF-IDF rather than
        raising: a missing model must not stop the server from starting, and
        the warning tells the operator that search quality is reduced.
        """
        if self.embedding_model == MODEL_TFIDF:
            from sklearn.feature_extraction.text import TfidfVectorizer

            self.vectorizer = TfidfVectorizer(
                max_features=384,  # Standard embedding size
                ngram_range=(1, 2),
                stop_words="english",
            )
            self._is_fitted = False
        elif self.embedding_model == MODEL_MINILM:
            try:
                from chromadb.utils import embedding_functions

                # onnxruntime ships with chromadb, so this pulls in no extra
                # dependency.
                self._embedding_function = (
                    embedding_functions.DefaultEmbeddingFunction()
                )

                # Constructing the function loads nothing: chromadb defers the
                # ~79MB download and the onnxruntime session to the first call.
                # So embed once here, where failure can still be handled. Doing
                # it lazily would raise from the middle of indexing, after the
                # caller had already stamped "minilm" onto the vector store --
                # leaving a store whose fingerprint disagrees with the vectors
                # that a degraded embedder would then produce.
                self._embedding_function(["probe"])

                logger.info("Loaded embedding model: all-MiniLM-L6-v2 (ONNX)")
            except Exception as e:
                # Broad by intent: an unavailable model shows up as an import
                # error, a download failure, an unwritable cache dir, or an
                # onnxruntime load error depending on the host, and every one of
                # them must degrade rather than abort startup.
                logger.warning(
                    f"Could not load the MiniLM embedding model ({e}); falling "
                    "back to TF-IDF. Semantic schema search will be much weaker "
                    "-- queries sharing no literal words with the schema return "
                    "results in index order. Set GRAPHRAG_EMBEDDING_MODEL=tfidf "
                    "to silence this, or restore network access to "
                    "~/.cache/chroma to use MiniLM."
                )
                self.embedding_model = MODEL_TFIDF
                self._initialize_model()
        elif self.embedding_model == MODEL_SENTENCE_TRANSFORMERS:
            try:
                from sentence_transformers import SentenceTransformer

                self.model = SentenceTransformer("all-MiniLM-L6-v2")
                logger.info("Loaded sentence-transformers model: all-MiniLM-L6-v2")
            except ImportError:
                logger.warning(
                    "sentence-transformers not available, falling back to MiniLM"
                )
                self.embedding_model = MODEL_MINILM
                self._initialize_model()

    def create_table_embedding(
        self,
        table_name: str,
        columns: list[dict[str, Any]],
        comment: str | None = None,
        foreign_keys: list[dict[str, Any]] | None = None,
    ) -> SchemaElement:
        """
        Create embedding for a table.

        Args:
            table_name: Name of the table
            columns: List of column metadata
            comment: Optional table comment/description
            foreign_keys: Optional foreign key relationships

        Returns:
            SchemaElement with embedding
        """
        element = self._describe_table(table_name, columns, comment, foreign_keys)
        element.embedding = self._embed_text(element.description)
        return element

    def _describe_table(
        self,
        table_name: str,
        columns: list[dict[str, Any]],
        comment: str | None = None,
        foreign_keys: list[dict[str, Any]] | None = None,
        schema: str | None = None,
    ) -> SchemaElement:
        """Build a table's element and its text, without embedding it.

        Separated from :meth:`create_table_embedding` so a whole schema's texts
        can be written first and inferred in batches afterwards.

        Args:
            table_name: Name of the table.
            columns: List of column metadata.
            comment: Optional table comment/description.
            foreign_keys: Optional foreign key relationships.

        Returns:
            SchemaElement whose ``embedding`` is still None.
        """
        # Build text representation
        text_parts = [table_name.replace("_", " ")]

        if comment:
            text_parts.append(comment)

        # Add column names and types
        for col in columns:
            col_text = f"{col['name']} {col['data_type']}"
            if col.get("comment"):
                col_text += f" {col['comment']}"
            text_parts.append(col_text)

        # Add relationship context
        if foreign_keys:
            for fk in foreign_keys:
                fk_text = f"relates to {fk['referenced_table']}"
                text_parts.append(fk_text)

        description = " ".join(text_parts)

        return SchemaElement(
            element_type="table",
            element_id=qualified(schema, table_name),
            name=table_name,
            description=description,
            metadata={
                "schema": schema,
                "table": qualified(schema, table_name),
                "table_name": table_name,
                "columns": [col["name"] for col in columns],
                "column_count": len(columns),
                "has_foreign_keys": bool(foreign_keys),
                "comment": comment,
            },
        )

    def create_column_embedding(
        self,
        table_name: str,
        column_name: str,
        data_type: str,
        is_primary_key: bool = False,
        is_foreign_key: bool = False,
        foreign_key_table: str | None = None,
        comment: str | None = None,
    ) -> SchemaElement:
        """
        Create embedding for a column.

        Args:
            table_name: Parent table name
            column_name: Column name
            data_type: SQL data type
            is_primary_key: Whether column is a primary key
            is_foreign_key: Whether column is a foreign key
            foreign_key_table: Referenced table if FK
            comment: Optional column comment

        Returns:
            SchemaElement with embedding
        """
        element = self._describe_column(
            table_name,
            column_name,
            data_type,
            is_primary_key,
            is_foreign_key,
            foreign_key_table,
            comment,
        )
        element.embedding = self._embed_text(element.description)
        return element

    def _describe_column(
        self,
        table_name: str,
        column_name: str,
        data_type: str,
        is_primary_key: bool = False,
        is_foreign_key: bool = False,
        foreign_key_table: str | None = None,
        comment: str | None = None,
        schema: str | None = None,
    ) -> SchemaElement:
        """Build a column's element and its text, without embedding it.

        Args:
            table_name: Parent table name.
            column_name: Column name.
            data_type: SQL data type.
            is_primary_key: Whether the column is a primary key.
            is_foreign_key: Whether the column is a foreign key.
            foreign_key_table: Referenced table if a foreign key.
            comment: Optional column comment.

        Returns:
            SchemaElement whose ``embedding`` is still None.
        """
        # Build text representation
        text_parts = [
            table_name.replace("_", " "),
            column_name.replace("_", " "),
            data_type,
        ]

        if comment:
            text_parts.append(comment)

        if is_primary_key:
            text_parts.append("primary key identifier")

        if is_foreign_key and foreign_key_table:
            text_parts.append(f"references {foreign_key_table}")

        description = " ".join(text_parts)

        return SchemaElement(
            element_type="column",
            element_id=f"{qualified(schema, table_name)}.{quote_part(column_name)}",
            name=column_name,
            description=description,
            metadata={
                # The qualified table, because this is what a query has to
                # name; the bare one beside it for display.
                "table": qualified(schema, table_name),
                "table_name": table_name,
                "schema": schema,
                "data_type": data_type,
                "is_primary_key": is_primary_key,
                "is_foreign_key": is_foreign_key,
                "foreign_key_table": foreign_key_table,
            },
        )

    def create_relationship_embedding(
        self,
        from_table: str,
        to_table: str,
        join_columns: list[tuple],
        relationship_type: str = "one_to_many",
    ) -> SchemaElement:
        """
        Create embedding for a relationship/join path.

        Args:
            from_table: Source table
            to_table: Target table
            join_columns: List of (from_col, to_col) tuples
            relationship_type: Type of relationship

        Returns:
            SchemaElement with embedding
        """
        element = self._describe_relationship(
            from_table, to_table, join_columns, relationship_type
        )
        element.embedding = self._embed_text(element.description)
        return element

    def _describe_relationship(
        self,
        from_table: str,
        to_table: str,
        join_columns: list[tuple],
        relationship_type: str = "one_to_many",
        from_schema: str | None = None,
        to_schema: str | None = None,
    ) -> SchemaElement:
        """Build a relationship's element and its text, without embedding it.

        Args:
            from_table: Source table.
            to_table: Target table.
            join_columns: List of (from_col, to_col) tuples.
            relationship_type: Type of relationship.

        Returns:
            SchemaElement whose ``embedding`` is still None.
        """
        # Build text representation
        join_desc = ", ".join([f"{fc} to {tc}" for fc, tc in join_columns])
        description = (
            f"{from_table} joins {to_table} on {join_desc} ({relationship_type})"
        )

        from_id = qualified(from_schema, from_table)
        to_id = qualified(to_schema, to_table)
        return SchemaElement(
            element_type="relationship",
            element_id=f"{from_id}__to__{to_id}",
            name=f"{from_table} → {to_table}",
            description=description,
            metadata={
                "from_table": from_id,
                "to_table": to_id,
                "from_table_name": from_table,
                "to_table_name": to_table,
                "join_columns": join_columns,
                "relationship_type": relationship_type,
            },
        )

    def _embed_text(self, text: str) -> np.ndarray:
        """
        Generate embedding for text.

        Args:
            text: Input text

        Returns:
            Embedding vector
        """
        if self.embedding_model == MODEL_MINILM:
            return np.asarray(self._embedding_function([text])[0], dtype=np.float32)
        if self.embedding_model == MODEL_SENTENCE_TRANSFORMERS:
            encoded = self.model.encode(text, convert_to_numpy=True)
            return np.asarray(encoded)

        # TF-IDF embedding
        if not self._is_fitted:
            # Fitting on one document leaves a vocabulary of just that
            # document's terms; batch_embed_schema() fits on the whole corpus
            # first, so this only covers a lone embed before any indexing.
            self.vectorizer.fit([text])
            self._is_fitted = True
            logger.warning(
                "TF-IDF embedded a text before any schema was indexed, so its "
                "vocabulary is that text's own words. A vector made this way "
                "cannot be compared with indexed ones."
            )

        embedding = self.vectorizer.transform([text]).toarray()[0]
        return np.asarray(embedding)

    def vocabulary_state(self) -> dict[str, Any] | None:
        """The fitted TF-IDF vocabulary, in a form that can be written to disk.

        A TF-IDF vector only means something against the vocabulary and
        document frequencies it was produced with. Those are fitted when a
        schema is indexed and were then lost with the process, so a restart
        left the stored vectors describing a space nothing could reproduce.

        Returns:
            The vocabulary, its inverse document frequencies and a fingerprint,
            or None for a backend that has no such state or is not fitted.
        """
        if self.embedding_model != MODEL_TFIDF or not self._is_fitted:
            return None
        try:
            vocabulary = {
                term: int(index) for term, index in self.vectorizer.vocabulary_.items()
            }
            idf = [float(value) for value in self.vectorizer.idf_]
        except AttributeError:  # pragma: no cover - not actually fitted
            return None
        return {
            "backend": MODEL_TFIDF,
            "vocabulary": vocabulary,
            "idf": idf,
            "fingerprint": vocabulary_fingerprint(vocabulary),
        }

    def load_vocabulary_state(self, state: dict[str, Any]) -> bool:
        """Restore a vocabulary saved by :meth:`vocabulary_state`.

        Restored rather than refitted: refitting on a different corpus gives a
        different space, and mixing spaces is what makes a search silently
        wrong rather than loudly broken.

        Args:
            state: What ``vocabulary_state`` produced.

        Returns:
            True if this embedder now embeds in the saved space.
        """
        if self.embedding_model != MODEL_TFIDF:
            return False
        vocabulary = state.get("vocabulary")
        idf = state.get("idf")
        if not vocabulary or not idf or len(vocabulary) != len(idf):
            logger.warning("Saved TF-IDF vocabulary is incomplete; not restoring it")
            return False

        # The file has to be the one the fingerprint describes. A half-written
        # or hand-edited vocabulary would embed queries in a space the stored
        # vectors do not share, which is the failure this whole mechanism
        # exists to prevent.
        expected = state.get("fingerprint")
        if expected and expected != vocabulary_fingerprint(vocabulary):
            logger.warning(
                "Saved TF-IDF vocabulary does not match its own fingerprint; "
                "not restoring it"
            )
            return False

        try:
            from sklearn.feature_extraction.text import TfidfVectorizer

            restored = TfidfVectorizer(
                max_features=384,
                stop_words="english",
                ngram_range=(1, 2),
                vocabulary=vocabulary,
            )
            # One fit builds the transformer the saved frequencies then replace.
            # The vocabulary is fixed by the constructor, so the placeholder
            # corpus cannot change which terms exist.
            restored.fit([" ".join(list(vocabulary)[:1]) or "placeholder"])
            restored.idf_ = np.asarray(idf, dtype=np.float64)
        except Exception as e:
            logger.warning(f"Could not restore the saved TF-IDF vocabulary: {e}")
            return False

        self.vectorizer = restored
        self._is_fitted = True
        logger.info(f"Restored a TF-IDF vocabulary of {len(vocabulary)} terms")
        return True

    def _embed_texts(self, texts: list[str]) -> list[np.ndarray]:
        """Embed many texts, in bounded batches, in the order given.

        Every backend is faster asked once for many texts than many times for
        one: the ONNX MiniLM session pays its per-call overhead once, and
        TF-IDF transforms one matrix instead of one row at a time. Vectors are
        identical either way -- both backends treat a row independently of the
        rest of the batch -- so this is inference cost only, not a change in
        what is indexed.

        Args:
            texts: Texts to embed.

        Returns:
            One vector per text, in the same order.
        """
        if not texts:
            return []

        if self.embedding_model == MODEL_MINILM:
            vectors: list[np.ndarray] = []
            for start in range(0, len(texts), EMBEDDING_BATCH_SIZE):
                batch = texts[start : start + EMBEDDING_BATCH_SIZE]
                vectors.extend(
                    np.asarray(vector, dtype=np.float32)
                    for vector in self._embedding_function(batch)
                )
            return vectors

        if self.embedding_model == MODEL_SENTENCE_TRANSFORMERS:
            encoded = self.model.encode(
                texts, convert_to_numpy=True, batch_size=EMBEDDING_BATCH_SIZE
            )
            return [np.asarray(vector) for vector in encoded]

        # TF-IDF. Fitting on this corpus rather than on one text is the same
        # concession _embed_text makes, one document wider.
        if not self._is_fitted:
            self.vectorizer.fit(texts)
            self._is_fitted = True

        matrix = self.vectorizer.transform(texts).toarray()
        return [np.asarray(row) for row in matrix]

    def _attach_embeddings(self, elements: list[SchemaElement]) -> None:
        """Embed the descriptions of already-built elements, in place.

        Args:
            elements: Elements from the ``_describe_*`` methods, whose
                ``embedding`` is still None.
        """
        if not elements:
            return

        vectors = self._embed_texts([element.description for element in elements])
        for element, vector in zip(elements, vectors, strict=True):
            element.embedding = vector

    def batch_embed_tables(
        self, tables_info: list[dict[str, Any]]
    ) -> list[SchemaElement]:
        """
        Create embeddings for multiple tables in batch.

        Args:
            tables_info: List of table metadata dictionaries

        Returns:
            List of SchemaElements with embeddings
        """
        elements = [
            self._describe_table(
                table_name=table["name"],
                columns=table.get("columns", []),
                comment=table.get("comment"),
                foreign_keys=table.get("foreign_keys", []),
                schema=table.get("schema"),
            )
            for table in tables_info
        ]
        self._attach_embeddings(elements)

        logger.info(f"Created embeddings for {len(elements)} tables")
        return elements

    def create_view_embedding(
        self,
        view_name: str,
        definition: str | None = None,
        comment: str | None = None,
        referenced_tables: list[str] | None = None,
    ) -> SchemaElement:
        """Create an embedding for a database view.

        The definition carries most of the signal. A view name states the
        business concept (``v_revenue_by_client``) and its body names the base
        tables, the measures and the join conditions an analyst already
        validated -- vocabulary that raw column names such as ``amount`` or
        ``unitcost`` do not carry on their own.

        Args:
            view_name: Name of the view.
            definition: The view's SQL body, if the backend exposed it.
            comment: Optional view comment.
            referenced_tables: Base tables the view reads, when known.

        Returns:
            SchemaElement of type "view".
        """
        element = self._describe_view(view_name, definition, comment, referenced_tables)
        element.embedding = self._embed_text(element.description)
        return element

    def _describe_view(
        self,
        view_name: str,
        definition: str | None = None,
        comment: str | None = None,
        referenced_tables: list[str] | None = None,
        schema: str | None = None,
    ) -> SchemaElement:
        """Build a view's element and its text, without embedding it.

        Args:
            view_name: Name of the view.
            definition: The view's SQL body, if the backend exposed it.
            comment: Optional view comment.
            referenced_tables: Base tables the view reads, when known.

        Returns:
            SchemaElement whose ``embedding`` is still None.
        """
        text_parts = [view_name.replace("_", " ")]

        if comment:
            text_parts.append(comment)

        if referenced_tables:
            text_parts.append("derived from " + " ".join(referenced_tables))

        if definition:
            # Underscores split so identifiers contribute their words:
            # "total_revenue" should match a query asking about revenue.
            text_parts.append(definition.replace("_", " "))

        description = " ".join(text_parts)

        return SchemaElement(
            element_type="view",
            element_id=qualified(schema, view_name),
            name=view_name,
            description=description,
            metadata={
                "schema": schema,
                "table": qualified(schema, view_name),
                "table_name": view_name,
                "definition": definition,
                "referenced_tables": referenced_tables or [],
                "comment": comment,
                "is_view": True,
            },
        )

    def batch_embed_schema(
        self,
        tables_info: list[dict[str, Any]],
        views_info: list[dict[str, Any]] | None = None,
    ) -> dict[str, list[SchemaElement]]:
        """
        Create embeddings for entire schema (tables, columns, relationships).

        Args:
            tables_info: List of table metadata
            views_info: Optional list of view metadata (name, definition,
                comment, referenced_tables). Views are indexed for search only;
                they are not part of the ontology.

        Returns:
            Dictionary with 'tables', 'columns', 'relationships', 'views' lists
        """
        result: dict[str, list[SchemaElement]] = {
            "tables": [],
            "columns": [],
            "relationships": [],
            "views": [],
        }

        # Collect all text for TF-IDF fitting if needed
        if self.embedding_model == "tfidf" and not self._is_fitted:
            all_texts = []
            for table in tables_info:
                text_parts = [table["name"]]
                if table.get("comment"):
                    text_parts.append(table["comment"])
                text_parts.extend(
                    f"{col['name']} {col['data_type']}"
                    for col in table.get("columns", [])
                )
                all_texts.append(" ".join(text_parts))

            # View bodies must be in the fitted vocabulary too, or TF-IDF
            # cannot match the very terms views were indexed to contribute.
            # The underscore splitting must match create_view_embedding
            # exactly: fitting on "profit_margin" while embedding
            # "profit margin" puts the element text outside the vocabulary it
            # was fitted against, and the term stays unmatchable either way.
            for view in views_info or []:
                view_texts = [view["name"].replace("_", " ")]
                if view.get("comment"):
                    view_texts.append(view["comment"])
                if view.get("definition"):
                    view_texts.append(view["definition"].replace("_", " "))
                all_texts.append(" ".join(view_texts))

            if all_texts:
                self.vectorizer.fit(all_texts)
                self._is_fitted = True

        # Create table embeddings
        for table in tables_info:
            # Table embedding
            table_element = self._describe_table(
                table_name=table["name"],
                columns=table.get("columns", []),
                comment=table.get("comment"),
                foreign_keys=table.get("foreign_keys", []),
                schema=table.get("schema"),
            )
            result["tables"].append(table_element)

            # Column embeddings
            for col in table.get("columns", []):
                col_element = self._describe_column(
                    table_name=table["name"],
                    column_name=col["name"],
                    data_type=col["data_type"],
                    is_primary_key=col.get("is_primary_key", False),
                    is_foreign_key=col.get("is_foreign_key", False),
                    foreign_key_table=col.get("foreign_key_table"),
                    comment=col.get("comment"),
                    schema=table.get("schema"),
                )
                result["columns"].append(col_element)

            # Relationship embeddings
            for fk in table.get("foreign_keys", []):
                rel_element = self._describe_relationship(
                    from_table=table["name"],
                    to_table=fk["referenced_table"],
                    join_columns=[(fk["column"], fk["referenced_column"])],
                    relationship_type="many_to_one",
                    from_schema=table.get("schema"),
                    to_schema=fk.get("referenced_schema") or table.get("schema"),
                )
                result["relationships"].append(rel_element)

        # View embeddings
        for view in views_info or []:
            view_element = self._describe_view(
                view_name=view["name"],
                definition=view.get("definition"),
                comment=view.get("comment"),
                referenced_tables=view.get("referenced_tables"),
                schema=view.get("schema"),
            )
            result["views"].append(view_element)

        # One inference pass over the whole schema, in bounded batches.
        self._attach_embeddings(
            [element for group in result.values() for element in group]
        )

        logger.info(
            f"Created embeddings for schema: "
            f"{len(result['tables'])} tables, "
            f"{len(result['columns'])} columns, "
            f"{len(result['relationships'])} relationships, "
            f"{len(result['views'])} views"
        )

        return result
