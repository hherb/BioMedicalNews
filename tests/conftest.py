"""Shared test fixtures."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bmnews.db.models import Base, create_tables


@pytest.fixture()
def db_session():
    """In-memory SQLite session for tests."""
    engine = create_engine("sqlite:///:memory:")
    create_tables(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()
    engine.dispose()
