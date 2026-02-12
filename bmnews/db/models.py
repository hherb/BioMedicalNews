"""SQLAlchemy ORM models."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

from sqlalchemy import (
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    types,
)
from sqlalchemy.orm import DeclarativeBase, relationship


# ---------------------------------------------------------------------------
# Custom column type: stores embeddings as JSON text on SQLite, or as a
# pgvector Vector on PostgreSQL.
# ---------------------------------------------------------------------------
class EmbeddingType(types.TypeDecorator):
    """Portable vector/embedding column.

    On PostgreSQL (with pgvector) this maps to ``Vector(dim)``.
    On SQLite this stores vectors as JSON-encoded lists of floats.
    """

    impl = types.Text
    cache_ok = True

    def __init__(self, dim: int = 384):
        super().__init__()
        self.dim = dim

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            try:
                from pgvector.sqlalchemy import Vector

                return dialect.type_descriptor(Vector(self.dim))
            except ImportError:
                pass
        return dialect.type_descriptor(types.Text())

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if dialect.name == "postgresql":
            return value  # pgvector accepts plain lists
        return json.dumps(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if dialect.name == "postgresql":
            if isinstance(value, str):
                return json.loads(value)
            return list(value)
        return json.loads(value)


class JSONListType(types.TypeDecorator):
    """Store a Python list as a JSON string."""

    impl = types.Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return json.dumps(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return json.loads(value)


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------
class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class Publication(Base):
    __tablename__ = "publications"

    id = Column(Integer, primary_key=True, autoincrement=True)
    doi = Column(String(255), unique=True, nullable=True, index=True)
    title = Column(Text, nullable=False)
    authors = Column(JSONListType)  # ["Author A", "Author B"]
    abstract = Column(Text)
    url = Column(Text)
    source = Column(String(50), index=True)  # "medrxiv", "biorxiv", "europepmc"
    published_date = Column(Date, index=True)
    fetched_date = Column(DateTime, default=_utcnow)
    categories = Column(JSONListType)
    metadata_json = Column(Text)  # Arbitrary extra metadata
    embedding = Column(EmbeddingType(384), nullable=True)

    scores = relationship("PublicationScore", back_populates="publication", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<Publication(id={self.id}, doi={self.doi!r}, title={self.title[:60]!r})>"


class UserProfile(Base):
    __tablename__ = "user_profiles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255))
    email = Column(String(255), nullable=False, unique=True)
    interests = Column(JSONListType)
    interest_embedding = Column(EmbeddingType(384), nullable=True)
    min_relevance = Column(Float, default=0.3)
    min_quality = Column(Float, default=0.2)

    scores = relationship("PublicationScore", back_populates="user", cascade="all, delete-orphan")
    digests = relationship("DigestRecord", back_populates="user", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<UserProfile(id={self.id}, email={self.email!r})>"


class PublicationScore(Base):
    __tablename__ = "publication_scores"
    __table_args__ = (
        UniqueConstraint("publication_id", "user_id", name="uq_pub_user"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    publication_id = Column(Integer, ForeignKey("publications.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("user_profiles.id"), nullable=False, index=True)
    relevance_score = Column(Float)
    quality_score = Column(Float)
    combined_score = Column(Float)
    scored_date = Column(DateTime, default=_utcnow)

    publication = relationship("Publication", back_populates="scores")
    user = relationship("UserProfile", back_populates="scores")


class DigestRecord(Base):
    __tablename__ = "digest_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("user_profiles.id"), nullable=False, index=True)
    sent_date = Column(DateTime, default=_utcnow)
    publication_ids = Column(JSONListType)  # [1, 2, 3]
    status = Column(String(20))  # "sent", "failed"

    user = relationship("UserProfile", back_populates="digests")


def create_tables(engine) -> None:
    """Create all tables (idempotent)."""
    Base.metadata.create_all(engine)
