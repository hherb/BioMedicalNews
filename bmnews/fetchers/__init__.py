"""Publication fetchers.

Every source bmnews can fetch from lives in bmlib's publication registry —
PubMed, bioRxiv, medRxiv and OpenAlex ship with bmlib, and Europe PMC is
contributed by :mod:`bmnews.fetchers.europepmc` and registered here via
:func:`bmlib.publications.register_source`.

Importing this package registers the bmnews-supplied sources, so a caller can
resolve any enabled source uniformly through
``bmlib.publications.get_fetcher(name)`` — there is no second, bmnews-local
dispatch path.
"""

from __future__ import annotations

from bmlib.publications import register_source
from bmlib.publications.models import SourceDescriptor, SourceParam

from bmnews.fetchers.base import FetchedPaper
from bmnews.fetchers.europepmc import SOURCE_NAME as EUROPEPMC
from bmnews.fetchers.europepmc import fetch_europepmc

EUROPEPMC_DESCRIPTOR = SourceDescriptor(
    name=EUROPEPMC,
    display_name="Europe PMC",
    description="Europe PMC literature and preprint search",
    params=[
        SourceParam(
            "query",
            "Europe PMC query string (default: all preprints, SRC:PPR)",
        ),
    ],
)


def register_local_sources() -> None:
    """Register the sources bmnews contributes to bmlib's registry.

    Idempotent — re-registering replaces the existing entry — so it is safe to
    call at import time and again from a test.
    """
    register_source(EUROPEPMC_DESCRIPTOR, fetch_europepmc)


register_local_sources()

__all__ = [
    "EUROPEPMC",
    "EUROPEPMC_DESCRIPTOR",
    "FetchedPaper",
    "fetch_europepmc",
    "register_local_sources",
]
