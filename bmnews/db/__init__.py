"""Database schema and operations for bmnews.

All SQL lives here — no direct SQL outside this package.
Uses bmlib.db for connection management and query execution.
"""

from bmnews.db.operations import (
    get_all_tags,
    get_paper_by_doi,
    get_paper_tags,
    get_papers_by_tag,
    get_papers_for_digest,
    get_scored_papers,
    get_unscored_papers,
    paper_exists,
    record_digest,
    save_paper_tags,
    save_score,
    upsert_paper,
)
from bmnews.db.schema import init_db

__all__ = [
    "init_db",
    "upsert_paper",
    "get_paper_by_doi",
    "get_unscored_papers",
    "save_score",
    "save_paper_tags",
    "get_paper_tags",
    "get_all_tags",
    "get_papers_by_tag",
    "get_scored_papers",
    "get_papers_for_digest",
    "record_digest",
    "paper_exists",
]
