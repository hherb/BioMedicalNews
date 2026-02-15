"""Integration test for full text retrieval and display."""

from unittest.mock import patch

import pytest

from bmlib.db import connect_sqlite
from bmlib.fulltext import FullTextResult
from bmnews.db.schema import init_db
from bmnews.db.operations import upsert_paper, save_score
from bmnews.gui.app import create_app
from bmnews.config import AppConfig


@pytest.fixture
def app_with_paper():
    config = AppConfig()
    conn = connect_sqlite(":memory:")
    init_db(conn)
    app = create_app(config, conn)
    app.config["TESTING"] = True

    pid = upsert_paper(
        conn, doi="10.1/integ", title="Integration Test Paper",
        authors="Smith J, Doe A", abstract="Background: Test. Methods: Test.",
        source="europepmc", metadata_json='{"pmid":"12345","pmcid":"PMC999"}',
    )
    save_score(conn, paper_id=pid, combined_score=0.8, relevance_score=0.9)

    return app, conn, pid


class TestEndToEnd:
    def test_reading_pane_shows_formatted_abstract(self, app_with_paper):
        app, conn, pid = app_with_paper
        with app.test_client() as client:
            resp = client.get(f"/papers/{pid}")
            assert resp.status_code == 200
            assert b"<strong>Background:</strong>" in resp.data

    def test_fulltext_button_present(self, app_with_paper):
        app, conn, pid = app_with_paper
        with app.test_client() as client:
            resp = client.get(f"/papers/{pid}")
            assert b"Get Full Text" in resp.data
            assert b"hx-post" in resp.data

    def test_fulltext_fetches_and_caches(self, app_with_paper):
        app, conn, pid = app_with_paper
        with app.test_client() as client:
            with patch("bmnews.gui.routes.papers.FullTextService") as MockSvc:
                instance = MockSvc.return_value
                instance.fetch_fulltext.return_value = FullTextResult(
                    source="europepmc",
                    html="<h2>Introduction</h2><p>Full text body.</p>",
                )
                resp = client.post(f"/papers/{pid}/fulltext")
                assert resp.status_code == 200
                assert b"Full text body" in resp.data

            # Second request should use cached version (no service call)
            resp2 = client.get(f"/papers/{pid}")
            assert b"Show Full Text" in resp2.data

    def test_pdf_url_cached_and_button_changes(self, app_with_paper):
        app, conn, pid = app_with_paper
        with app.test_client() as client:
            with patch("bmnews.gui.routes.papers.FullTextService") as MockSvc:
                instance = MockSvc.return_value
                instance.fetch_fulltext.return_value = FullTextResult(
                    source="unpaywall",
                    pdf_url="https://example.com/paper.pdf",
                )
                resp = client.post(f"/papers/{pid}/fulltext")
                assert resp.status_code == 200
                assert b"Open PDF" in resp.data

            # Reading pane should now show "Open PDF" link, not "Get Full Text"
            resp2 = client.get(f"/papers/{pid}")
            assert b"Open PDF" in resp2.data
            assert b"Get Full Text" not in resp2.data
