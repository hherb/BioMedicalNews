"""Database schema and operations for bmnews.

All SQL lives here — no direct SQL outside this package.
Uses bmlib.db for connection management and query execution, and
bmlib.publications for the paper records themselves.
"""

from bmnews.db.operations import (
    count_unscored_papers,
    get_all_tags,
    get_cached_digest_papers,
    get_fulltext_sources,
    get_paper,
    get_paper_by_doi,
    get_paper_metadata,
    get_paper_tags,
    get_paper_with_score,
    get_papers_by_tag,
    get_papers_filtered,
    get_papers_for_digest,
    get_scored_papers,
    get_unscored_papers,
    paper_exists,
    paper_url,
    publication_id,
    record_digest,
    save_fulltext,
    save_paper_metadata,
    save_paper_tags,
    save_score,
    store_paper,
)
from bmnews.db.schema import init_db, open_db

__all__ = [
    # Schema
    "init_db",
    "open_db",
    # Papers
    "store_paper",
    "publication_id",
    "paper_exists",
    "paper_url",
    "get_paper",
    "get_paper_by_doi",
    "get_paper_with_score",
    "get_papers_filtered",
    "get_unscored_papers",
    "count_unscored_papers",
    # Scores
    "save_score",
    "get_scored_papers",
    "get_papers_for_digest",
    "get_cached_digest_papers",
    # Digests
    "record_digest",
    # Paper extras
    "save_fulltext",
    "get_fulltext_sources",
    "save_paper_metadata",
    "get_paper_metadata",
    # Tags
    "save_paper_tags",
    "get_paper_tags",
    "get_all_tags",
    "get_papers_by_tag",
]
