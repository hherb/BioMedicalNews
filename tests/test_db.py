"""Tests for the database models and repository."""

from __future__ import annotations

from datetime import date, datetime, timezone

from bmnews.db.models import Publication, PublicationScore, UserProfile
from bmnews.db import repository as repo


class TestPublicationUpsert:
    def test_insert_new(self, db_session):
        pub = Publication(doi="10.1101/001", title="Paper 1", source="medrxiv")
        result = repo.upsert_publication(db_session, pub)
        db_session.flush()
        assert result.id is not None
        assert result.doi == "10.1101/001"

    def test_deduplicate_by_doi(self, db_session):
        pub1 = Publication(doi="10.1101/002", title="Paper A", source="medrxiv")
        repo.upsert_publication(db_session, pub1)
        db_session.flush()

        pub2 = Publication(doi="10.1101/002", title="Paper A duplicate", source="biorxiv")
        result = repo.upsert_publication(db_session, pub2)
        assert result.title == "Paper A"  # Original kept

    def test_insert_without_doi(self, db_session):
        pub = Publication(doi=None, title="No DOI paper", source="europepmc")
        result = repo.upsert_publication(db_session, pub)
        db_session.flush()
        assert result.id is not None


class TestUserProfile:
    def test_create_user(self, db_session):
        user = repo.get_or_create_user(
            db_session, name="Test", email="test@example.com",
            interests=["genomics", "AI"],
        )
        assert user.id is not None
        assert user.interests == ["genomics", "AI"]

    def test_update_existing_user(self, db_session):
        repo.get_or_create_user(
            db_session, name="Test", email="test@example.com",
            interests=["old interest"],
        )
        db_session.flush()

        user = repo.get_or_create_user(
            db_session, name="Test Updated", email="test@example.com",
            interests=["new interest"],
        )
        assert user.name == "Test Updated"
        assert user.interests == ["new interest"]


class TestScoring:
    def test_save_and_retrieve_scores(self, db_session):
        pub = Publication(doi="10.1101/010", title="Scored paper", source="medrxiv")
        repo.upsert_publication(db_session, pub)
        db_session.flush()

        user = repo.get_or_create_user(
            db_session, name="U", email="u@example.com", interests=["test"],
        )
        db_session.flush()

        score = PublicationScore(
            publication_id=pub.id, user_id=user.id,
            relevance_score=0.8, quality_score=0.6, combined_score=0.72,
        )
        repo.save_score(db_session, score)
        db_session.flush()

        top = repo.get_top_scored(db_session, user.id, exclude_sent=False)
        assert len(top) == 1
        assert top[0][1].relevance_score == 0.8

    def test_unscored_publications(self, db_session):
        pub1 = Publication(doi="10.1101/020", title="P1", source="medrxiv")
        pub2 = Publication(doi="10.1101/021", title="P2", source="medrxiv")
        repo.upsert_publication(db_session, pub1)
        repo.upsert_publication(db_session, pub2)
        db_session.flush()

        user = repo.get_or_create_user(
            db_session, name="U", email="u2@example.com", interests=[],
        )
        db_session.flush()

        # Score only the first paper
        repo.save_score(db_session, PublicationScore(
            publication_id=pub1.id, user_id=user.id,
            relevance_score=0.5, quality_score=0.5, combined_score=0.5,
        ))
        db_session.flush()

        unscored = repo.get_unscored_publications(db_session, user.id)
        assert len(unscored) == 1
        assert unscored[0].title == "P2"


class TestVectorSimilarity:
    def test_cosine_similarity(self):
        from bmnews.db.repository import _cosine_similarity
        a = [1.0, 0.0, 0.0]
        b = [1.0, 0.0, 0.0]
        assert abs(_cosine_similarity(a, b) - 1.0) < 1e-6

        c = [0.0, 1.0, 0.0]
        assert abs(_cosine_similarity(a, c)) < 1e-6

    def test_sqlite_vector_search(self, db_session):
        pub = Publication(
            doi="10.1101/vec", title="Vector paper", source="medrxiv",
            embedding=[0.5, 0.5, 0.0],
        )
        repo.upsert_publication(db_session, pub)
        db_session.flush()

        results = repo.find_similar_publications(
            db_session, [0.5, 0.5, 0.0], limit=5, threshold=0.5,
        )
        assert len(results) == 1
        assert results[0][1] > 0.99  # Near-perfect match
