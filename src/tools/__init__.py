"""
MCP Tools for OrionBelt Analytics.

DEPRECATED: This package is superseded by ``src/handlers/``.
All tool logic has been moved to the ``handlers`` package which uses
per-session dependency injection instead of the shared global
``get_db_manager()`` pattern.

Only ``tools/chart.py`` remains actively used (imported by
``handlers/chart.py``). The other modules in this package are kept
for backward compatibility but should NOT be used for new development.

Migration guide:
    tools/connection.py  -> handlers/connection.py
    tools/schema.py      -> handlers/schema.py
    tools/ontology.py    -> handlers/ontology.py
    tools/query.py       -> handlers/query.py
    tools/info.py        -> handlers/info.py
    tools/chart.py       -> (still active, used by handlers/chart.py)
"""

import warnings as _warnings

_warnings.warn(
    "src.tools package is deprecated. Use src.handlers instead.",
    DeprecationWarning,
    stacklevel=2,
)

# Only re-export chart which is still actively used
from .chart import generate_chart

__all__ = [
    'generate_chart',
]