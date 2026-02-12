"""Relevance scoring — how well a publication matches user interests.

Two strategies are provided:

1. **KeywordRelevanceScorer** (default) — pure-Python keyword/phrase matching
   against the title and abstract.  Zero extra dependencies.

2. **SemanticRelevanceScorer** — cosine similarity of dense embeddings
   produced by sentence-transformers.  Requires ``bmnews[semantic]``.
"""

from __future__ import annotations

import logging
import math
import re
from abc import ABC, abstractmethod

from bmnews.fetchers.base import FetchedPaper

logger = logging.getLogger(__name__)


class RelevanceScorer(ABC):
    """Base class for relevance scorers."""

    @abstractmethod
    def score(self, paper: FetchedPaper, interests: list[str]) -> float:
        """Return a relevance score in [0, 1]."""
        ...

    @abstractmethod
    def compute_embedding(self, text: str) -> list[float] | None:
        """Return a dense embedding vector, or None if not supported."""
        ...


# ---------------------------------------------------------------------------
# Keyword scorer
# ---------------------------------------------------------------------------

class KeywordRelevanceScorer(RelevanceScorer):
    """Score relevance via keyword / phrase matching.

    For each user interest string we look for exact and partial matches in the
    title and abstract.  Title matches are weighted higher.  The final score
    is the fraction of interests that matched, boosted by match quality.
    """

    TITLE_WEIGHT = 3.0
    ABSTRACT_WEIGHT = 1.0

    def score(self, paper: FetchedPaper, interests: list[str]) -> float:
        if not interests:
            return 0.0

        title = paper.title.lower()
        abstract = paper.abstract.lower() if paper.abstract else ""
        categories = " ".join(paper.categories).lower()
        full_text = f"{title} {abstract} {categories}"

        total = 0.0
        for interest in interests:
            interest_lower = interest.lower()
            tokens = interest_lower.split()

            # Exact phrase match
            title_exact = interest_lower in title
            abstract_exact = interest_lower in abstract

            # Token overlap (partial match)
            title_tokens = set(re.findall(r"\w+", title))
            abstract_tokens = set(re.findall(r"\w+", abstract))
            interest_tokens = set(re.findall(r"\w+", interest_lower))

            if not interest_tokens:
                continue

            title_overlap = len(interest_tokens & title_tokens) / len(interest_tokens)
            abstract_overlap = len(interest_tokens & abstract_tokens) / len(interest_tokens)

            # Combine: exact phrase match is worth more than partial token overlap
            interest_score = 0.0
            if title_exact:
                interest_score = max(interest_score, 1.0 * self.TITLE_WEIGHT)
            if abstract_exact:
                interest_score = max(interest_score, 1.0 * self.ABSTRACT_WEIGHT)
            interest_score = max(interest_score, title_overlap * self.TITLE_WEIGHT * 0.6)
            interest_score = max(interest_score, abstract_overlap * self.ABSTRACT_WEIGHT * 0.6)

            # Normalise this interest's contribution to [0, 1]
            max_possible = self.TITLE_WEIGHT
            total += min(interest_score / max_possible, 1.0)

        # Average across all interests
        raw = total / len(interests)
        return min(raw, 1.0)

    def compute_embedding(self, text: str) -> list[float] | None:
        return None


# ---------------------------------------------------------------------------
# Semantic scorer (optional dependency)
# ---------------------------------------------------------------------------

class SemanticRelevanceScorer(RelevanceScorer):
    """Score relevance via dense embedding cosine similarity.

    Requires the ``sentence-transformers`` package (``pip install bmnews[semantic]``).
    """

    def __init__(self, model_name: str = "all-MiniLM-L6-v2"):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError:
            raise ImportError(
                "SemanticRelevanceScorer requires sentence-transformers. "
                "Install with: pip install bmnews[semantic]"
            )
        logger.info("Loading sentence-transformer model: %s", model_name)
        self._model = SentenceTransformer(model_name)

    def score(self, paper: FetchedPaper, interests: list[str]) -> float:
        if not interests:
            return 0.0
        paper_text = f"{paper.title}. {paper.abstract or ''}"
        interest_text = ". ".join(interests)

        paper_emb = self._model.encode(paper_text, normalize_embeddings=True)
        interest_emb = self._model.encode(interest_text, normalize_embeddings=True)

        sim = float(paper_emb @ interest_emb)
        # Map from [-1, 1] cosine similarity to [0, 1] score
        return max(0.0, min(1.0, (sim + 1) / 2))

    def compute_embedding(self, text: str) -> list[float]:
        emb = self._model.encode(text, normalize_embeddings=True)
        return emb.tolist()


def get_scorer(name: str = "keyword") -> RelevanceScorer:
    """Factory: return the requested scorer."""
    if name == "keyword":
        return KeywordRelevanceScorer()
    if name == "semantic":
        return SemanticRelevanceScorer()
    raise ValueError(f"Unknown scorer: {name!r}  (use 'keyword' or 'semantic')")
