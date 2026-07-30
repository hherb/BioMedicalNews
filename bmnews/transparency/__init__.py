"""Research-integrity analysis for stored papers.

Wraps :mod:`bmlib.transparency` as a pipeline stage. Named alongside it without
ambiguity because every import in bmnews is absolute.
"""

from __future__ import annotations

from bmnews.transparency.service import (
    TransparencyReport,
    build_settings,
    list_results,
    run_transparency,
)

__all__ = [
    "TransparencyReport",
    "build_settings",
    "list_results",
    "run_transparency",
]
