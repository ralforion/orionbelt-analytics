"""A multilingual sentence-embedding model, run through ONNX.

all-MiniLM-L6-v2 was trained on English. A German question barely reaches
English schema names -- "Umsatz" against a column named "Net revenue" scores
0.06 -- and German schemas are common in exactly the setups this server is
deployed into. paraphrase-multilingual-MiniLM-L12-v2 maps 50+ languages into
one space ("Umsatz" -> "Net revenue": 0.40), with the same 384 dimensions.

Run the way chromadb runs MiniLM: the model's own tokenizer, an ONNX session,
mean pooling over the tokens, L2 normalisation. No PyTorch: tokenizers,
onnxruntime and huggingface_hub already come with chromadb.

The files are fetched from a pinned Hugging Face revision and each is checked
against a pinned SHA-256 before it is used -- the same guarantee chromadb
gives for MiniLM. The 8-bit quantised export is used: 118 MB instead of 470,
with the same rankings in measurement, and it runs on x86 and ARM alike.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

REPOSITORY = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
REVISION = "e8f8c211226b894fcb81acc59f3b34ba3efd5f42"
MODEL_FILE = "onnx/model_quint8_avx2.onnx"
TOKENIZER_FILE = "tokenizer.json"
SHA256 = {
    MODEL_FILE: "98a01d88b7de996cdea58c32ca71208c09968d143798814b2ea09d3439dc334f",
    TOKENIZER_FILE: "2c3387be76557bd40970cec13153b3bbf80407865484b209e655e5e4729076b8",
}
# sentence_bert_config.json of the pinned revision.
MAX_SEQUENCE_LENGTH = 128
DIMENSION = 384

# Paths already checked in this process: hashing 118 MB is ~0.3 s, worth
# paying once, not per embedder.
_verified: dict[str, Path] = {}


class ModelIntegrityError(RuntimeError):
    """A downloaded model file does not match its pinned checksum."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def model_files() -> dict[str, Path]:
    """The model's files, downloaded once into the Hugging Face cache, verified.

    A file already in the cache is not fetched again; the revision is a commit
    hash, so the cached copy is the one asked for.

    Returns:
        Local paths keyed by file name in the repository.

    Raises:
        ModelIntegrityError: If a file's SHA-256 is not the pinned one.
        Exception: If a file cannot be fetched, e.g. without network access
            and with nothing cached.
    """
    from huggingface_hub import hf_hub_download

    paths: dict[str, Path] = {}
    for name, expected in SHA256.items():
        held = _verified.get(name)
        if held is not None and held.exists():
            paths[name] = held
            continue
        path = Path(hf_hub_download(REPOSITORY, name, revision=REVISION))
        actual = _sha256(path)
        if actual != expected:
            raise ModelIntegrityError(
                f"{REPOSITORY}/{name} has SHA-256 {actual}, expected {expected}; "
                "refusing to load it"
            )
        _verified[name] = path
        paths[name] = path
    return paths


class MultilingualEmbedding:
    """Encodes texts with paraphrase-multilingual-MiniLM-L12-v2.

    Called like chromadb's embedding functions: a list of texts in, one
    normalised 384-dimensional vector per text out.
    """

    def __init__(self) -> None:
        """Fetch, verify and load the model and its tokenizer.

        Raises:
            ModelIntegrityError: If a file fails its checksum.
            Exception: If the files cannot be fetched or loaded.
        """
        import onnxruntime as ort
        from tokenizers import Tokenizer

        files = model_files()
        self._tokenizer = Tokenizer.from_file(str(files[TOKENIZER_FILE]))
        self._tokenizer.enable_truncation(MAX_SEQUENCE_LENGTH)
        self._tokenizer.enable_padding()
        self._session = ort.InferenceSession(
            str(files[MODEL_FILE]), providers=["CPUExecutionProvider"]
        )
        self._inputs = {i.name for i in self._session.get_inputs()}

    def __call__(self, texts: list[str]) -> list[np.ndarray]:
        """Embed a batch of texts.

        Args:
            texts: The texts.

        Returns:
            One float32 vector of length 384 per text, unit length.
        """
        if not texts:
            return []
        encoded = self._tokenizer.encode_batch(texts)
        ids = np.array([e.ids for e in encoded], dtype=np.int64)
        mask = np.array([e.attention_mask for e in encoded], dtype=np.int64)
        feed: dict[str, Any] = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self._inputs:
            feed["token_type_ids"] = np.zeros_like(ids)
        tokens = self._session.run(None, feed)[0]
        # Mean over real tokens only (1_Pooling/config.json: mean tokens).
        weights = mask[..., None].astype(np.float32)
        pooled = (tokens * weights).sum(axis=1) / np.clip(
            weights.sum(axis=1), 1e-9, None
        )
        norms = np.linalg.norm(pooled, axis=1, keepdims=True)
        vectors = pooled / np.clip(norms, 1e-12, None)
        return [np.asarray(v, dtype=np.float32) for v in vectors]
