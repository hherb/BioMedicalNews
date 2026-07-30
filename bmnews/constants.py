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


def _tier_scores() -> dict[str, float]:
    """Derive a 0.0–1.0 score per :class:`bmlib.quality.QualityTier`.

    bmlib already defines the evidence hierarchy (``DESIGN_TO_TIER``) and the
    0–10 score of every study design (``DESIGN_TO_SCORE``); restating it here
    as a second table would drift the moment bmlib re-weights a design.  Each
    tier takes the strongest design that maps to it, rescaled to 0.0–1.0.

    Returns:
        Tier name → score, for tiers with at least one scored design.
    """
    from bmlib.quality import DESIGN_TO_SCORE, DESIGN_TO_TIER

    scores: dict[str, float] = {}
    for design, tier in DESIGN_TO_TIER.items():
        score = DESIGN_TO_SCORE.get(design, 0.0) / QUALITY_SCORE_SCALE
        if score > scores.get(tier.name, 0.0):
            scores[tier.name] = score
    return scores


#: Approximate 0.0–1.0 score for each :class:`bmlib.quality.QualityTier`, used
#: only when an assessment carries a tier but no explicit numeric score.
QUALITY_TIER_SCORES: dict[str, float] = _tier_scores()

#: Quality assessment tiers understood by :func:`bmnews.scoring.scorer._build_quality_filter`.
QUALITY_TIER_METADATA_ONLY: int = 1
QUALITY_TIER_LLM_CLASSIFIER: int = 2
QUALITY_TIER_DEEP_ASSESSMENT: int = 3

# --- LLM --------------------------------------------------------------------

#: Defaults matching :class:`bmlib.agents.BaseAgent`, so bmnews and bmlib
#: agree on generation settings when the user has configured none.
DEFAULT_TEMPERATURE: float = 0.3
DEFAULT_MAX_TOKENS: int = 4096

# --- Database ---------------------------------------------------------------

#: Number of unscored papers pulled into a single scoring run.
UNSCORED_BATCH_SIZE: int = 500

#: Default ``LIMIT`` for ad-hoc paper queries that don't paginate.
DEFAULT_QUERY_LIMIT: int = 100

#: Where migration 4 writes any ``papers`` row it could not carry across to
#: ``publications``. That migration drops ``papers``, so a stranded row is
#: otherwise unrecoverable — this file is the record of what was lost.
STRANDED_PAPERS_PATH: str = "~/.bmnews/stranded-papers.json"

#: How many stranded rows are named individually in the log before the rest
#: are summarised. The rescue file always holds every one of them.
STRANDED_PAPERS_LOG_LIMIT: int = 20

#: How many buffered source-extras entries may accumulate during a sync before
#: they are flushed to ``paper_extras``.  bmlib stores a day's records only
#: after that day's fetch finishes, so a flush cannot resolve records still in
#: flight; this caps the buffer at roughly one busy day rather than the whole
#: lookback window.
EXTRAS_FLUSH_THRESHOLD: int = 1000

# --- Notifications ----------------------------------------------------------

#: How many papers one watch delivers in a single run when the watch does not
#: say. The rest stay in the derived pending queue and can be pulled on demand,
#: so this bounds a batch rather than discarding anything.
DEFAULT_NOTIFY_MAX_PER_RUN: int = 5

#: How many candidate rows one scan of the pending queue reads. SQL narrows and
#: orders; the criteria the matcher applies in Python can still reject rows, so
#: the queue is assembled by scanning successive chunks until one comes back
#: short. A scan window, never a delivery cap — see
#: ``bmnews.notify.service.collect_matches``. Sized so the usual case is one
#: round trip and a first run over an established corpus is a handful, since
#: the scan runs to exhaustion either way and the columns it reads are narrow.
NOTIFY_SCAN_CHUNK: int = 500

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

# --- Transparency -----------------------------------------------------------

#: How many papers one transparency run analyses. bmlib paces every outbound
#: request 0.35 s apart across the whole analyzer, and one analysis makes four
#: to eight of them (CrossRef, a Europe PMC search, its full text, PubMed
#: efetch, OpenAlex, up to three ClinicalTrials.gov lookups) — so 1.4–2.8 s per
#: paper is mandatory however many threads run. Concurrency hides per-request
#: latency; it cannot raise that ceiling. This bounds a run to a few minutes
#: and leaves the rest queued, exactly as ``UNSCORED_BATCH_SIZE`` does.
TRANSPARENCY_BATCH_SIZE: int = 100

#: How many times a paper whose analysis came back UNKNOWN is re-attempted
#: before it is left alone. bmlib sets its reachability flag only on an HTTP
#: 200, so it reports UNREACHABLE both for a network outage and for a paper
#: indexed in none of its five APIs — the two are indistinguishable. Retrying
#: every UNKNOWN forever would therefore re-query every unindexed preprint on
#: every run. ``bmnews transparency --refresh`` resets the count.
TRANSPARENCY_MAX_ATTEMPTS: int = 3
