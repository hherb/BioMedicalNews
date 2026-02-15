"""Publication fetchers for preprint servers and literature databases.

Registry-based sources (PubMed, bioRxiv, medRxiv, OpenAlex) are provided by
bmlib and accessed via ``bmlib.publications.fetchers.get_fetcher()``.

Local sources that are not part of bmlib (e.g. Europe PMC) remain here.
"""

from bmnews.fetchers.base import FetchedPaper
from bmnews.fetchers.europepmc import fetch_europepmc

__all__ = [
    "FetchedPaper",
    "fetch_europepmc",
]
