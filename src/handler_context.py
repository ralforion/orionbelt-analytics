"""Typed request-scoped services passed to handler functions.

Previously each tool wrapper in ``src/main.py`` threaded a handful of helper
callables into its handler as separate keyword arguments (``get_session_data``,
``create_error_response``, ``get_oxigraph_store``, ...). That made every wrapper
and every handler signature long and easy to get out of sync.

:class:`HandlerContext` bundles those services into one typed object. A wrapper
builds it once (capturing the current module-level helpers, so test patches on
``src.main`` / ``src.server_state`` are still honored) and passes ``services=``;
each handler reads what it needs as ``services.<name>``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional


@dataclass(frozen=True)
class HandlerContext:
    """Request-scoped services available to handler functions.

    All fields are optional so a wrapper only needs to populate the services its
    handler actually uses; unused ones stay ``None``.
    """

    get_session_data: Optional[Callable] = None
    get_session_db_manager: Optional[Callable] = None
    get_session_safe_filename: Optional[Callable] = None
    get_session_obqc_validator: Optional[Callable] = None
    get_oxigraph_store: Optional[Callable] = None
    load_ontology_from_session: Optional[Callable] = None
    create_error_response: Optional[Callable] = None
    server_state: Any = None
    get_connection_fingerprint: Optional[Callable] = None
    clear_session_state: Optional[Callable] = None
    auto_initialize_graphrag_background: Optional[Callable] = None
    add_resource: Optional[Callable] = None
