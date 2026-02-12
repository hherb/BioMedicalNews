"""Quality heuristic scoring for preprints.

Since preprints lack formal peer-review signals, we estimate quality from
textual cues in the abstract and available metadata.  The heuristics are
intentionally conservative — they flag *potential* quality, not certainty.

Scoring components (each yields 0–1, then weighted):

1. Abstract completeness — structured sections present
2. Methodological rigour — mentions of study design, statistics, sample sizes
3. Reporting quality — effect sizes, confidence intervals, p-values
4. Author / collaboration signals — author count, institutions
5. Length & substance — abstract length as a rough proxy
"""

from __future__ import annotations

import re

from bmnews.fetchers.base import FetchedPaper

# Component weights (must sum to 1.0)
W_COMPLETENESS = 0.25
W_METHODOLOGY = 0.25
W_REPORTING = 0.20
W_COLLABORATION = 0.15
W_SUBSTANCE = 0.15

# ---- Keyword lists --------------------------------------------------------

_SECTION_KEYWORDS = [
    "background", "introduction", "objective", "objectives", "aim", "aims",
    "method", "methods", "methodology", "design", "study design",
    "result", "results", "finding", "findings", "outcome", "outcomes",
    "conclusion", "conclusions", "discussion", "interpretation",
]

_METHODOLOGY_KEYWORDS = [
    "randomized", "randomised", "controlled trial", "rct",
    "cohort", "case-control", "cross-sectional", "longitudinal",
    "meta-analysis", "systematic review", "prospective", "retrospective",
    "regression", "multivariate", "univariate", "bayesian",
    "machine learning", "deep learning", "neural network",
    "sample size", "power analysis", "bootstrap",
    "cox proportional", "kaplan-meier", "hazard ratio",
    "sensitivity analysis", "subgroup analysis",
    "inclusion criteria", "exclusion criteria",
    "pre-registered", "preregistered",
    "blinded", "double-blind", "placebo",
]

_REPORTING_PATTERNS = [
    r"\bp\s*[<=<]\s*0\.\d+",  # p-values
    r"\bCI\b",  # confidence interval
    r"confidence\s+interval",
    r"\bOR\b\s*[=:]",  # odds ratio
    r"\bHR\b\s*[=:]",  # hazard ratio
    r"\bRR\b\s*[=:]",  # relative risk
    r"effect\s+size",
    r"\bAUC\b",  # area under curve
    r"\bROC\b",  # receiver operating characteristic
    r"n\s*=\s*\d{2,}",  # sample size mentions (n=100+)
    r"\d+\s*participants",
    r"\d+\s*patients",
    r"\d+\s*subjects",
]


def score_quality(paper: FetchedPaper) -> float:
    """Return a quality heuristic score in [0, 1]."""
    abstract = (paper.abstract or "").lower()
    if not abstract:
        return 0.1  # Minimal score for papers with no abstract

    completeness = _score_completeness(abstract)
    methodology = _score_methodology(abstract)
    reporting = _score_reporting(abstract)
    collaboration = _score_collaboration(paper)
    substance = _score_substance(abstract)

    total = (
        completeness * W_COMPLETENESS
        + methodology * W_METHODOLOGY
        + reporting * W_REPORTING
        + collaboration * W_COLLABORATION
        + substance * W_SUBSTANCE
    )
    return min(total, 1.0)


def _score_completeness(abstract: str) -> float:
    """How many structured-abstract section keywords are present?"""
    found = sum(1 for kw in _SECTION_KEYWORDS if kw in abstract)
    # Expect roughly 4 key sections for a well-structured abstract
    return min(found / 4.0, 1.0)


def _score_methodology(abstract: str) -> float:
    """How many methodological keywords are mentioned?"""
    found = sum(1 for kw in _METHODOLOGY_KEYWORDS if kw in abstract)
    # 3+ methodology mentions is a good sign
    return min(found / 3.0, 1.0)


def _score_reporting(abstract: str) -> float:
    """Presence of quantitative reporting patterns."""
    found = sum(1 for pat in _REPORTING_PATTERNS if re.search(pat, abstract, re.IGNORECASE))
    return min(found / 3.0, 1.0)


def _score_collaboration(paper: FetchedPaper) -> float:
    """Rough collaboration quality from author count."""
    n_authors = len(paper.authors) if paper.authors else 0
    if n_authors == 0:
        return 0.2
    if n_authors == 1:
        return 0.3
    if n_authors <= 3:
        return 0.5
    if n_authors <= 8:
        return 0.7
    return 1.0  # Large collaborations tend to have more rigorous processes


def _score_substance(abstract: str) -> float:
    """Abstract length as a crude proxy for substance."""
    word_count = len(abstract.split())
    if word_count < 50:
        return 0.2
    if word_count < 150:
        return 0.5
    if word_count < 300:
        return 0.8
    return 1.0
