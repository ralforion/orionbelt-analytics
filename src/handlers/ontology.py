"""Ontology handlers facade.

The implementation is split by concern into focused modules; this module
re-exports the public handler functions so callers (``src/main.py`` and tests)
keep importing them from ``src.handlers.ontology`` unchanged:

- :mod:`.ontology_generation` - generate ontology from schema
- :mod:`.ontology_semantic`   - suggest / apply semantic names
- :mod:`.ontology_io`         - load custom .ttl ontologies
- :mod:`.ontology_artifacts`  - download ontology / R2RML

``OUTPUT_DIR`` and ``ensure_output_dir`` are re-exported for the test harness
(conftest redirects OUTPUT_DIR; some tests patch ensure_output_dir).
"""

from ..paths import OUTPUT_DIR, ensure_output_dir
from .ontology_artifacts import download_ontology, download_r2rml
from .ontology_generation import generate_ontology
from .ontology_io import load_my_ontology
from .ontology_semantic import apply_semantic_names, suggest_semantic_names

__all__ = [
    "generate_ontology",
    "suggest_semantic_names",
    "apply_semantic_names",
    "load_my_ontology",
    "download_ontology",
    "download_r2rml",
    "OUTPUT_DIR",
    "ensure_output_dir",
]
