"""Main orchestration pipeline.

Coordinates the full workflow:
  1. Fetch new publications from configured sources
  2. Store them in the database (deduplicated by DOI)
  3. Score each publication for relevance and quality
  4. Select papers above thresholds
  5. Render and send an email digest
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from bmnews.config import AppConfig
from bmnews.db.engine import get_session
from bmnews.db.models import Publication, PublicationScore, create_tables
from bmnews.db import repository as repo
from bmnews.digest.renderer import render_html, render_text
from bmnews.digest.sender import send_digest
from bmnews.fetchers.base import FetchedPaper
from bmnews.fetchers.europepmc import EuropePMCFetcher
from bmnews.fetchers.medrxiv import MedRxivFetcher
from bmnews.scoring.quality import score_quality
from bmnews.scoring.relevance import get_scorer

logger = logging.getLogger(__name__)


def run(config: AppConfig) -> dict:
    """Execute the full pipeline. Returns a summary dict."""
    from bmnews.db.engine import get_engine, init_engine

    engine = init_engine(config)
    create_tables(engine)

    summary: dict = {"fetched": 0, "scored": 0, "digest_size": 0, "email_sent": False}

    since = date.today() - timedelta(days=config.sources.lookback_days)

    # --- 1. Fetch ---
    fetched_papers = _fetch_all(config, since)
    summary["fetched"] = len(fetched_papers)
    logger.info("Fetched %d papers total", len(fetched_papers))

    # --- 2. Store ---
    with get_session() as session:
        stored = _store_papers(session, fetched_papers)
        logger.info("Stored %d new papers", len(stored))

    # --- 3. Ensure user profile exists ---
    with get_session() as session:
        user = repo.get_or_create_user(
            session,
            name=config.user.name,
            email=config.user.email,
            interests=config.user.interests,
            min_relevance=config.scoring.min_relevance,
            min_quality=config.scoring.min_quality,
        )
        user_id = user.id

    # --- 4. Score ---
    scorer = get_scorer(config.scoring.scorer)
    with get_session() as session:
        unscored = repo.get_unscored_publications(session, user_id)
        user = session.get(UserProfile, user_id)
        interests = user.interests or []
        logger.info("Scoring %d unscored publications", len(unscored))

        for pub in unscored:
            paper_proxy = FetchedPaper(
                doi=pub.doi, title=pub.title, authors=pub.authors or [],
                abstract=pub.abstract or "", url=pub.url or "",
                source=pub.source or "", published_date=pub.published_date,
                categories=pub.categories or [],
            )
            rel = scorer.score(paper_proxy, interests)
            qual = score_quality(paper_proxy)
            combined = 0.6 * rel + 0.4 * qual

            # Optionally store embedding
            if pub.embedding is None:
                emb = scorer.compute_embedding(f"{pub.title}. {pub.abstract or ''}")
                if emb is not None:
                    pub.embedding = emb

            repo.save_score(session, PublicationScore(
                publication_id=pub.id,
                user_id=user_id,
                relevance_score=rel,
                quality_score=qual,
                combined_score=combined,
            ))
        summary["scored"] = len(unscored)

    # --- 5. Build digest ---
    with get_session() as session:
        top = repo.get_top_scored(
            session, user_id,
            min_relevance=config.scoring.min_relevance,
            min_quality=config.scoring.min_quality,
            limit=50,
            exclude_sent=True,
        )
        summary["digest_size"] = len(top)

        if not top:
            logger.info("No papers above threshold — skipping digest")
            return summary

        logger.info("Digest: %d papers above threshold", len(top))

        html = render_html(top)
        text = render_text(top)
        pub_ids = [pub.id for pub, _ in top]

        # --- 6. Send email ---
        if config.email.smtp_host and config.email.smtp_user:
            ok = send_digest(
                config=config.email,
                to_address=config.user.email,
                subject=f"BioMedical News Digest — {date.today().strftime('%B %d, %Y')}",
                html_body=html,
                text_body=text,
            )
            summary["email_sent"] = ok
            status = "sent" if ok else "failed"
        else:
            logger.info("SMTP not configured — writing digest to stdout")
            print(text)
            status = "printed"

        repo.record_digest(session, user_id, pub_ids, status)

    return summary


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _fetch_all(config: AppConfig, since: date) -> list[FetchedPaper]:
    papers: list[FetchedPaper] = []
    if config.sources.medrxiv:
        papers.extend(MedRxivFetcher("medrxiv").fetch(since))
    if config.sources.biorxiv:
        papers.extend(MedRxivFetcher("biorxiv").fetch(since))
    if config.sources.europepmc:
        papers.extend(EuropePMCFetcher().fetch(since))
    return papers


def _store_papers(session, papers: list[FetchedPaper]) -> list[Publication]:
    stored = []
    for fp in papers:
        pub = Publication(
            doi=fp.doi,
            title=fp.title,
            authors=fp.authors,
            abstract=fp.abstract,
            url=fp.url,
            source=fp.source,
            published_date=fp.published_date,
            categories=fp.categories,
            metadata_json=None,
        )
        pub = repo.upsert_publication(session, pub)
        stored.append(pub)
    return stored


# Re-export for convenience
from bmnews.db.models import UserProfile  # noqa: E402
