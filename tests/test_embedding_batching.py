"""Indexing embeds in batches, and the result is what per-element embedding gave.

Asking a backend once for many texts costs far less than once per text: a
20-table, 12-column schema took 17.8 s through MiniLM one element at a time and
2.8 s batched. That is only worth having if nothing about the index changes, so
these tests compare batched output against the per-element methods element by
element -- ids, descriptions, metadata and vectors.
"""

from typing import Any

import numpy as np
import pytest

from src.graphrag.embedder import (
    EMBEDDING_BATCH_SIZE,
    SchemaElement,
    SchemaEmbedder,
)


def _schema(tables: int = 3, columns: int = 4) -> list[dict[str, Any]]:
    return [
        {
            "name": f"table_{t}",
            "comment": f"business entity {t}",
            "columns": [
                {
                    "name": f"col_{c}",
                    "data_type": "VARCHAR",
                    "comment": "amount in euro" if c == 2 else None,
                    "is_primary_key": c == 0,
                    "is_foreign_key": False,
                }
                for c in range(columns)
            ],
            "foreign_keys": (
                []
                if t == 0
                else [
                    {
                        "column": "col_1",
                        "referenced_table": "table_0",
                        "referenced_column": "col_0",
                    }
                ]
            ),
        }
        for t in range(tables)
    ]


_VIEWS = [
    {
        "name": "v_total_revenue",
        "definition": "SELECT sum(amount) FROM table_0",
        "comment": "revenue per client",
        "referenced_tables": ["table_0"],
    }
]


def _per_element(
    embedder: SchemaEmbedder,
    tables: list[dict[str, Any]],
    views: list[dict[str, Any]],
) -> list[SchemaElement]:
    """What indexing did before batching: one inference call per element."""
    tables_out, columns, relationships, views_out = [], [], [], []
    for table in tables:
        tables_out.append(
            embedder.create_table_embedding(
                table["name"],
                table["columns"],
                table.get("comment"),
                table.get("foreign_keys", []),
            )
        )
        columns.extend(
            embedder.create_column_embedding(
                table["name"],
                col["name"],
                col["data_type"],
                col.get("is_primary_key", False),
                col.get("is_foreign_key", False),
                col.get("foreign_key_table"),
                col.get("comment"),
            )
            for col in table["columns"]
        )
        relationships.extend(
            embedder.create_relationship_embedding(
                table["name"],
                fk["referenced_table"],
                [(fk["column"], fk["referenced_column"])],
                "many_to_one",
            )
            for fk in table.get("foreign_keys", [])
        )
    views_out.extend(
        embedder.create_view_embedding(
            view["name"],
            view.get("definition"),
            view.get("comment"),
            view.get("referenced_tables"),
        )
        for view in views
    )
    return tables_out + columns + relationships + views_out


class TestBatchedMatchesPerElement:
    """The batched pass produces exactly what the singular methods produce."""

    def test_tfidf_schema_is_unchanged(self) -> None:
        tables = _schema()
        batched = SchemaEmbedder("tfidf").batch_embed_schema(tables, _VIEWS)

        reference_embedder = SchemaEmbedder("tfidf")
        # Fit the vectorizer over the same corpus, the way indexing does.
        reference_embedder.batch_embed_schema(tables, _VIEWS)
        reference = _per_element(reference_embedder, tables, _VIEWS)

        produced = [
            element
            for group in ("tables", "columns", "relationships", "views")
            for element in batched[group]
        ]
        assert len(produced) == len(reference)
        for element, expected in zip(produced, reference, strict=True):
            assert element.element_id == expected.element_id
            assert element.element_type == expected.element_type
            assert element.description == expected.description
            assert element.metadata == expected.metadata
            assert element.embedding is not None
            assert expected.embedding is not None
            assert np.array_equal(element.embedding, expected.embedding)

    def test_tfidf_tables_only_is_unchanged(self) -> None:
        tables = _schema()
        embedder = SchemaEmbedder("tfidf")
        batched = embedder.batch_embed_tables(tables)

        reference_embedder = SchemaEmbedder("tfidf")
        reference_embedder.vectorizer = embedder.vectorizer
        reference_embedder._is_fitted = True
        reference = [
            reference_embedder.create_table_embedding(
                table["name"],
                table["columns"],
                table.get("comment"),
                table.get("foreign_keys", []),
            )
            for table in tables
        ]

        for element, expected in zip(batched, reference, strict=True):
            assert element.description == expected.description
            assert element.embedding is not None
            assert expected.embedding is not None
            assert np.array_equal(element.embedding, expected.embedding)


class TestInferenceIsBatched:
    """The backend is called once per bounded batch, not once per element."""

    def test_a_schema_takes_one_inference_call(self) -> None:
        embedder = SchemaEmbedder("tfidf")
        calls: list[int] = []
        original = embedder._embed_texts

        def counting(texts: list[str]) -> list[np.ndarray]:
            calls.append(len(texts))
            return original(texts)

        embedder._embed_texts = counting  # type: ignore[method-assign]
        result = embedder.batch_embed_schema(_schema(), _VIEWS)

        total = sum(len(group) for group in result.values())
        assert calls == [total]

    def test_batches_are_bounded(self) -> None:
        """More elements than the bound means several calls, none over it."""
        embedder = SchemaEmbedder("tfidf")
        texts = [f"text number {i}" for i in range(EMBEDDING_BATCH_SIZE * 2 + 7)]
        embedder.vectorizer.fit(texts)
        embedder._is_fitted = True

        sizes: list[int] = []
        # TF-IDF transforms in one matrix; MiniLM is the backend that chunks.
        embedder.embedding_model = "minilm"

        def counting_inference(batch: list[str]) -> list[np.ndarray]:
            sizes.append(len(batch))
            return [np.zeros(384, dtype=np.float32) for _ in batch]

        embedder._embedding_function = counting_inference  # type: ignore[assignment]
        vectors = embedder._embed_texts(texts)

        assert len(vectors) == len(texts)
        assert sum(sizes) == len(texts)
        assert max(sizes) <= EMBEDDING_BATCH_SIZE
        assert len(sizes) == 3

    def test_no_texts_asks_the_backend_nothing(self) -> None:
        embedder = SchemaEmbedder("tfidf")

        def refuse(batch: list[str]) -> list[np.ndarray]:
            raise AssertionError("the backend should not have been called")

        embedder._embedding_function = refuse  # type: ignore[assignment]
        assert embedder._embed_texts([]) == []


class TestOrderIsPreserved:
    """A vector must land on the element whose text produced it."""

    def test_vectors_follow_the_order_of_the_texts(self) -> None:
        embedder = SchemaEmbedder("tfidf")
        embedder.embedding_model = "minilm"

        def one_per_text(batch: list[str]) -> list[np.ndarray]:
            # A distinguishable vector per text: its length.
            return [np.full(384, float(len(text)), dtype=np.float32) for text in batch]

        embedder._embedding_function = one_per_text  # type: ignore[assignment]
        texts = ["a", "bb", "ccc", "dddd"]

        vectors = embedder._embed_texts(texts)

        assert [float(vector[0]) for vector in vectors] == [1.0, 2.0, 3.0, 4.0]

    def test_a_backend_returning_the_wrong_count_is_an_error(self) -> None:
        embedder = SchemaEmbedder("tfidf")
        embedder.embedding_model = "minilm"
        embedder._embedding_function = lambda batch: [  # type: ignore[assignment]
            np.zeros(384, dtype=np.float32)
        ]

        elements = [embedder._describe_table("t", [{"name": "c", "data_type": "INT"}])]
        elements.append(embedder._describe_table("u", []))

        with pytest.raises(ValueError):
            embedder._attach_embeddings(elements)
