"""Pure criteria matching: does this paper satisfy this watch?

No database, no HTTP, no LLM — the whole module is a predicate over the paper
dict :func:`bmnews.db.operations._row_to_paper` already produces, so every
criterion can be tested against a literal dict with no fixtures at all.

That purity is also what makes the selection split in
:mod:`bmnews.notify.service` safe to reason about. SQL does the indexable
narrowing (score floors, the not-already-sent anti-join, the ordering) and this
module applies the rest — keyword substrings, tag and source sets, journal,
study design. The two overlap on the score floors deliberately: re-checking
them here costs nothing and keeps this function a complete statement of what a
watch means, so it can be used on its own for a dry run.

The one field the paper dict does not carry natively is ``tags``: interest tags
live in the ``paper_tags`` table, not on ``publications``. Callers join them in
before matching, and a paper without them simply fails any tag criterion.
"""

from __future__ import annotations

from typing import Any

from bmnews.notify.watches import Watch
from bmnews.scoring.scorer import tiers_below


def matches(paper: dict[str, Any], watch: Watch) -> bool:
    """Report whether *paper* satisfies every criterion of *watch*.

    Criteria are AND-combined and an unset one imposes no constraint, so a
    watch with nothing configured matches everything. Within one list
    criterion the test is ``any``: ``tags = ["a", "b"]`` means either tag.

    ``enabled`` is not consulted here — whether to evaluate a watch at all is
    the caller's decision, and a dry run wants to see what a disabled watch
    would match.

    Args:
        paper: A paper dict as produced by ``_row_to_paper``, with score
            columns joined and, for tag criteria, a ``tags`` sequence.
        watch: The watch to test against.

    Returns:
        True if every configured criterion holds.
    """
    return (
        _score_of(paper, "relevance_score") >= watch.min_relevance
        and _score_of(paper, "combined_score") >= watch.min_combined
        and _tier_ok(paper, watch)
        and _any_of(watch.tags, paper.get("tags"))
        and _any_of(watch.sources, paper.get("sources"))
        and _any_of(watch.journals, [paper.get("journal")])
        and _any_of(watch.study_designs, [paper.get("study_design")])
        and _keywords_ok(paper, watch)
    )


def _score_of(paper: dict[str, Any], key: str) -> float:
    """Read a score column, treating an unscored paper as zero.

    Watches are evaluated after scoring, so a missing score should not happen;
    if it does, zero means "fails any floor" rather than raising mid-run.
    """
    value = paper.get(key)
    return float(value) if value is not None else 0.0


def _tier_ok(paper: dict[str, Any], watch: Watch) -> bool:
    """Apply the quality-tier floor, exempting ``UNCLASSIFIED``.

    The floor is expressed as the set of tiers ranked below it, via
    :func:`bmnews.scoring.scorer.tiers_below`, so the evidence hierarchy is
    read from bmlib in exactly one place and the carve-out matches what the
    digest already does: a paper the classifier could not place is unjudged,
    not judged-and-rejected, and excluding it would silently hide every
    unfamiliar study design.
    """
    if not watch.min_quality_tier:
        return True
    return (paper.get("quality_tier") or "") not in set(tiers_below(watch.min_quality_tier))


def _keywords_ok(paper: dict[str, Any], watch: Watch) -> bool:
    """Match any keyword as a case-insensitive substring of title or abstract.

    Substring rather than word matching is deliberate: these are the user's own
    search terms, and "melanoma" should find "melanomas".
    """
    if not watch.keywords:
        return True
    haystack = f"{paper.get('title') or ''}\n{paper.get('abstract') or ''}".lower()
    return any(keyword.lower() in haystack for keyword in watch.keywords)


def _any_of(wanted: tuple[str, ...], present: Any) -> bool:
    """Report whether *present* holds any of *wanted*, case-insensitively.

    An empty *wanted* imposes no constraint. A missing or empty *present* fails
    a criterion that was configured — the paper does not carry what was asked
    for.
    """
    if not wanted:
        return True
    have = {str(item).strip().lower() for item in (present or []) if item}
    return any(item.strip().lower() in have for item in wanted)
