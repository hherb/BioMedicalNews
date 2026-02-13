"""Publication fetchers for preprint servers and literature databases."""

from bmnews.fetchers.base import FetchedPaper
from bmnews.fetchers.medrxiv import fetch_medrxiv, fetch_biorxiv
from bmnews.fetchers.europepmc import fetch_europepmc

__all__ = [
    "FetchedPaper",
    "fetch_medrxiv",
    "fetch_biorxiv",
    "fetch_europepmc",
]
