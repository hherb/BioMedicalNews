"""Data access layer with vector-search abstraction."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from typing import Sequence

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from bmnews.db.models import DigestRecord, Publication, PublicationScore, UserProfile

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Publication helpers
# ---------------------------------------------------------------------------

def upsert_publication(session: Session, pub: Publication) -> Publication:
    """Insert a publication or return the existing one (matched by DOI)."""
    if pub.doi:
        existing = session.execute(
            select(Publication).where(Publication.doi == pub.doi)
        ).scalar_one_or_none()
        if existing is not None:
            return existing
    session.add(pub)
    session.flush()
    return pub


def get_unscored_publications(session: Session, user_id: int) -> Sequence[Publication]:
    """Return publications that have not been scored for the given user."""
    scored_ids = (
        select(PublicationScore.publication_id)
        .where(PublicationScore.user_id == user_id)
        .scalar_subquery()
    )
    return list(
        session.execute(
            select(Publication).where(Publication.id.notin_(scored_ids))
        ).scalars().all()
    )


def get_publications_since(session: Session, since: date) -> Sequence[Publication]:
    """Return publications fetched since a given date."""
    return list(
        session.execute(
            select(Publication).where(Publication.published_date >= since)
        ).scalars().all()
    )


# ---------------------------------------------------------------------------
# Score helpers
# ---------------------------------------------------------------------------

def save_score(session: Session, score: PublicationScore) -> PublicationScore:
    """Insert or update a publication score."""
    existing = session.execute(
        select(PublicationScore).where(
            PublicationScore.publication_id == score.publication_id,
            PublicationScore.user_id == score.user_id,
        )
    ).scalar_one_or_none()
    if existing:
        existing.relevance_score = score.relevance_score
        existing.quality_score = score.quality_score
        existing.combined_score = score.combined_score
        existing.scored_date = datetime.now(timezone.utc)
        session.flush()
        return existing
    session.add(score)
    session.flush()
    return score


def get_top_scored(
    session: Session,
    user_id: int,
    *,
    min_relevance: float = 0.0,
    min_quality: float = 0.0,
    limit: int = 50,
    exclude_sent: bool = True,
) -> list[tuple[Publication, PublicationScore]]:
    """Return top-scoring publications for a user, optionally excluding already-sent ones."""
    query = (
        select(Publication, PublicationScore)
        .join(PublicationScore, PublicationScore.publication_id == Publication.id)
        .where(
            PublicationScore.user_id == user_id,
            PublicationScore.relevance_score >= min_relevance,
            PublicationScore.quality_score >= min_quality,
        )
        .order_by(PublicationScore.combined_score.desc())
        .limit(limit)
    )

    if exclude_sent:
        # Find publication IDs already sent in a digest for this user
        sent_records = session.execute(
            select(DigestRecord.publication_ids).where(DigestRecord.user_id == user_id)
        ).scalars().all()
        sent_pub_ids: set[int] = set()
        for ids_json in sent_records:
            if ids_json:
                sent_pub_ids.update(ids_json)
        if sent_pub_ids:
            query = query.where(Publication.id.notin_(sent_pub_ids))

    rows = session.execute(query).all()
    return [(pub, score) for pub, score in rows]


# ---------------------------------------------------------------------------
# User helpers
# ---------------------------------------------------------------------------

def get_or_create_user(session: Session, *, name: str, email: str, interests: list[str],
                       min_relevance: float = 0.3, min_quality: float = 0.2) -> UserProfile:
    """Get existing user by email or create a new one."""
    user = session.execute(
        select(UserProfile).where(UserProfile.email == email)
    ).scalar_one_or_none()
    if user:
        user.name = name
        user.interests = interests
        user.min_relevance = min_relevance
        user.min_quality = min_quality
        session.flush()
        return user
    user = UserProfile(
        name=name, email=email, interests=interests,
        min_relevance=min_relevance, min_quality=min_quality,
    )
    session.add(user)
    session.flush()
    return user


# ---------------------------------------------------------------------------
# Digest helpers
# ---------------------------------------------------------------------------

def record_digest(session: Session, user_id: int, publication_ids: list[int], status: str) -> DigestRecord:
    rec = DigestRecord(
        user_id=user_id,
        publication_ids=publication_ids,
        status=status,
    )
    session.add(rec)
    session.flush()
    return rec


# ---------------------------------------------------------------------------
# Vector similarity search (backend-aware)
# ---------------------------------------------------------------------------

def find_similar_publications(
    session: Session,
    query_embedding: list[float],
    *,
    limit: int = 20,
    threshold: float = 0.5,
) -> list[tuple[Publication, float]]:
    """Find publications similar to a query embedding.

    Uses pgvector operators on PostgreSQL and falls back to in-Python
    cosine similarity on SQLite.
    """
    dialect = session.bind.dialect.name if session.bind else "sqlite"

    if dialect == "postgresql":
        return _pg_vector_search(session, query_embedding, limit, threshold)
    return _sqlite_vector_search(session, query_embedding, limit, threshold)


def _pg_vector_search(
    session: Session, query_embedding: list[float], limit: int, threshold: float
) -> list[tuple[Publication, float]]:
    """Cosine similarity search using pgvector."""
    vec_literal = "[" + ",".join(str(v) for v in query_embedding) + "]"
    sql = text(
        "SELECT id, 1 - (embedding <=> :vec) AS similarity "
        "FROM publications "
        "WHERE embedding IS NOT NULL "
        "AND 1 - (embedding <=> :vec) >= :threshold "
        "ORDER BY similarity DESC "
        "LIMIT :lim"
    )
    rows = session.execute(sql, {"vec": vec_literal, "threshold": threshold, "lim": limit}).all()
    pub_ids = [r[0] for r in rows]
    sims = {r[0]: r[1] for r in rows}
    pubs = list(session.execute(select(Publication).where(Publication.id.in_(pub_ids))).scalars().all())
    return [(p, sims[p.id]) for p in pubs]


def _sqlite_vector_search(
    session: Session, query_embedding: list[float], limit: int, threshold: float
) -> list[tuple[Publication, float]]:
    """Brute-force cosine similarity in Python for SQLite."""
    pubs = list(
        session.execute(
            select(Publication).where(Publication.embedding.isnot(None))
        ).scalars().all()
    )
    results: list[tuple[Publication, float]] = []
    for pub in pubs:
        emb = pub.embedding
        if not emb:
            continue
        sim = _cosine_similarity(query_embedding, emb)
        if sim >= threshold:
            results.append((pub, sim))
    results.sort(key=lambda x: x[1], reverse=True)
    return results[:limit]


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors without numpy."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)
