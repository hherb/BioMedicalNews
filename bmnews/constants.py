"""Shared constants for bmnews.

Central home for values that would otherwise be scattered as magic numbers
across the fetch → store → score → digest pipeline and the GUI.  Anything
that is genuinely user-tunable belongs in :mod:`bmnews.config` instead;
this module holds the fixed values that define application behaviour.
"""

from __future__ import annotations

# --- HTTP -------------------------------------------------------------------

#: Default timeout (seconds) for outbound HTTP requests to publication APIs.
HTTP_TIMEOUT_SECONDS: float = 30.0

#: Page size for the Europe PMC REST search endpoint (API maximum is 1000).
EUROPEPMC_PAGE_SIZE: int = 100

#: Page size for the medRxiv/bioRxiv details endpoint (fixed by the API).
RXIV_PAGE_SIZE: int = 100

#: Safety valve: maximum number of result pages to walk before giving up.
#: Guards against an API that keeps handing back a fresh cursor forever.
MAX_FETCH_PAGES: int = 200

# --- Scoring ----------------------------------------------------------------

#: Weight of the LLM relevance score in the combined score.
RELEVANCE_WEIGHT: float = 0.6

#: Weight of the quality assessment in the combined score.
QUALITY_WEIGHT: float = 0.4

#: Divisor mapping bmlib's 0–10 quality score onto the 0.0–1.0 range.
QUALITY_SCORE_SCALE: float = 10.0

#: Fallback score used when a paper's quality tier is unknown.
DEFAULT_QUALITY_SCORE: float = 0.3

#: Approximate 0.0–1.0 score for each :class:`bmlib.quality.QualityTier`.
QUALITY_TIER_SCORES: dict[str, float] = {
    "UNCLASSIFIED": 0.3,
    "TIER_1_ANECDOTAL": 0.3,
    "TIER_2_OBSERVATIONAL": 0.5,
    "TIER_3_CONTROLLED": 0.7,
    "TIER_4_EXPERIMENTAL": 0.85,
    "TIER_5_SYNTHESIS": 0.95,
}

#: Quality assessment tiers understood by :func:`bmnews.scoring.scorer._build_quality_filter`.
QUALITY_TIER_METADATA_ONLY: int = 1
QUALITY_TIER_LLM_CLASSIFIER: int = 2
QUALITY_TIER_DEEP_ASSESSMENT: int = 3

#: LLM provider names recognised by bmlib, used to tell a ``provider:model``
#: string apart from a bare Ollama ``model:tag`` string.
KNOWN_LLM_PROVIDERS: frozenset[str] = frozenset(
    {"anthropic", "ollama", "openai", "deepseek", "mistral", "gemini"}
)

# --- Database ---------------------------------------------------------------

#: Number of unscored papers pulled into a single scoring run.
UNSCORED_BATCH_SIZE: int = 500

#: Default ``LIMIT`` for ad-hoc paper queries that don't paginate.
DEFAULT_QUERY_LIMIT: int = 100

# --- GUI --------------------------------------------------------------------

#: Number of papers per page in the GUI list and CLI search results.
DEFAULT_PAGE_SIZE: int = 20

#: Combined-score thresholds for the colour of a paper card's score badge.
SCORE_BADGE_HIGH: float = 0.7
SCORE_BADGE_MID: float = 0.5

#: Fallback contact address sent to Europe PMC / Unpaywall when the user has
#: not configured one (both APIs ask for an identifying email).
DEFAULT_CONTACT_EMAIL: str = "bmnews@example.com"

#: Default desktop window geometry, used when no saved state exists.
DEFAULT_WINDOW_WIDTH: int = 1200
DEFAULT_WINDOW_HEIGHT: int = 800
MIN_WINDOW_WIDTH: int = 600
MIN_WINDOW_HEIGHT: int = 400

#: Seconds to wait for the embedded Flask server to accept connections
#: before opening the native window anyway.
SERVER_START_TIMEOUT_SECONDS: float = 5.0

#: Interval (seconds) between readiness probes while waiting for the server.
SERVER_POLL_INTERVAL_SECONDS: float = 0.05

#: Truncation length for paper titles printed by ``bmnews search``.
CLI_TITLE_TRUNCATE: int = 80
