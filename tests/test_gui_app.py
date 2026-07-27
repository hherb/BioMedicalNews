"""Tests for the GUI Flask app."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from bmlib.db import connect_sqlite
from bmlib.fulltext import FullTextResult

from bmnews.config import AppConfig
from bmnews.db.operations import get_paper_by_doi, save_score, store_paper
from bmnews.db.schema import init_db


@pytest.fixture
def app():
    from bmnews.gui.app import create_app
    config = AppConfig()
    conn = connect_sqlite(":memory:")
    init_db(conn)
    app = create_app(config, conn)
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def seeded_client(app):
    conn = app.config["BMNEWS_DB"]
    p1 = store_paper(conn, doi="10.1101/g1", title="Alpha Paper",
                     authors=["Smith J"], abstract="Cancer immunotherapy.",
                     source="medrxiv", published_date="2026-02-10")
    save_score(conn, paper_id=p1, relevance_score=0.9, quality_score=0.8,
               combined_score=0.86, summary="A strong trial.",
               study_design="rct", quality_tier="TIER_4_EXPERIMENTAL")

    p2 = store_paper(conn, doi="10.1101/g2", title="Beta Paper",
                     authors=["Jones K"], abstract="Genomics study.",
                     source="biorxiv", published_date="2026-02-12")
    save_score(conn, paper_id=p2, relevance_score=0.6, quality_score=0.5,
               combined_score=0.56, summary="Interesting cohort.",
               study_design="cohort", quality_tier="TIER_3_CONTROLLED")
    return app.test_client()


class TestAppFactory:
    def test_creates_flask_app(self, app):
        assert app is not None

    def test_index_returns_200(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert b"Bio-Medical News" in resp.data

    def test_static_files_served(self, client):
        resp = client.get("/static/vendor/htmx.min.js")
        assert resp.status_code == 200


class TestPapersRoute:
    def test_papers_list(self, seeded_client):
        resp = seeded_client.get("/papers")
        assert resp.status_code == 200
        assert b"Alpha Paper" in resp.data
        assert b"Beta Paper" in resp.data

    def test_papers_sorted_by_date(self, seeded_client):
        resp = seeded_client.get("/papers?sort=date")
        assert resp.status_code == 200
        alpha_pos = resp.data.index(b"Alpha Paper")
        beta_pos = resp.data.index(b"Beta Paper")
        assert beta_pos < alpha_pos

    def test_papers_filter_by_source(self, seeded_client):
        resp = seeded_client.get("/papers?source=medrxiv")
        assert resp.status_code == 200
        assert b"Alpha Paper" in resp.data
        assert b"Beta Paper" not in resp.data

    def test_paper_detail(self, seeded_client):
        conn = seeded_client.application.config["BMNEWS_DB"]
        paper = get_paper_by_doi(conn, "10.1101/g1")
        resp = seeded_client.get(f"/papers/{paper['id']}")
        assert resp.status_code == 200
        assert b"Alpha Paper" in resp.data
        assert b"Cancer immunotherapy" in resp.data
        assert b"A strong trial" in resp.data

    def test_paper_detail_not_found(self, seeded_client):
        resp = seeded_client.get("/papers/99999")
        assert resp.status_code == 404

    def test_search(self, seeded_client):
        resp = seeded_client.get("/search?q=immunotherapy")
        assert resp.status_code == 200
        assert b"Alpha Paper" in resp.data
        assert b"Beta Paper" not in resp.data


class TestSettingsRoute:
    def test_settings_page(self, client):
        resp = client.get("/settings")
        assert resp.status_code == 200
        assert b"Sources" in resp.data or b"sources" in resp.data

    def test_save_settings(self, client):
        resp = client.post("/settings/save", data={
            "sources.lookback_days": "14",
            "scoring.min_relevance": "0.6",
        })
        assert resp.status_code == 200
        config = client.application.config["BMNEWS_CONFIG"]
        assert config.sources.lookback_days == 14
        assert config.scoring.min_relevance == 0.6

    def test_template_list(self, client):
        resp = client.get("/settings/templates")
        assert resp.status_code == 200

    def test_template_load(self, client):
        resp = client.get("/settings/template/digest_email.html")
        assert resp.status_code == 200


class TestPipelineRoute:
    def test_run_pipeline_returns_status(self, client):
        import time
        with patch("bmnews.pipeline.run_pipeline") as mock_run:
            resp = client.post("/pipeline/run")
            assert resp.status_code == 200
            assert b"pipeline" in resp.data.lower()
            # Background thread — give it a moment to start
            for _ in range(20):
                if mock_run.called:
                    break
                time.sleep(0.05)
            mock_run.assert_called_once()

    def test_pipeline_status_route(self, client):
        resp = client.get("/pipeline/status")
        assert resp.status_code == 200


class TestEndToEnd:
    def test_full_workflow(self, seeded_client):
        resp = seeded_client.get("/")
        assert resp.status_code == 200
        assert b"Bio-Medical News" in resp.data

        resp = seeded_client.get("/papers")
        assert resp.status_code == 200
        assert b"Alpha Paper" in resp.data

        conn = seeded_client.application.config["BMNEWS_DB"]
        paper = get_paper_by_doi(conn, "10.1101/g1")
        resp = seeded_client.get(f"/papers/{paper['id']}")
        assert resp.status_code == 200
        assert b"Cancer immunotherapy" in resp.data

        resp = seeded_client.get("/search?q=Genomics")
        assert resp.status_code == 200
        assert b"Beta Paper" in resp.data
        assert b"Alpha Paper" not in resp.data

        resp = seeded_client.get("/settings")
        assert resp.status_code == 200

        resp = seeded_client.post("/settings/save", data={
            "sources.lookback_days": "30",
        })
        assert resp.status_code == 200
        config = seeded_client.application.config["BMNEWS_CONFIG"]
        assert config.sources.lookback_days == 30


class TestFullTextRoute:
    def test_fulltext_endpoint_exists(self, seeded_client):
        conn = seeded_client.application.config["BMNEWS_DB"]
        paper = get_paper_by_doi(conn, "10.1101/g1")
        with patch("bmnews.gui.routes.papers.FullTextService") as mock_svc:
            instance = mock_svc.return_value
            instance.fetch_fulltext.return_value = FullTextResult(
                source="europepmc", html="<p>Full text content</p>",
            )
            resp = seeded_client.post(f"/papers/{paper['id']}/fulltext")
            assert resp.status_code == 200

    def test_fulltext_returns_html_fragment(self, seeded_client):
        conn = seeded_client.application.config["BMNEWS_DB"]
        paper = get_paper_by_doi(conn, "10.1101/g1")
        with patch("bmnews.gui.routes.papers.FullTextService") as mock_svc:
            instance = mock_svc.return_value
            instance.fetch_fulltext.return_value = FullTextResult(
                source="europepmc", html="<p>Full text content</p>",
            )
            resp = seeded_client.post(f"/papers/{paper['id']}/fulltext")
            assert resp.status_code == 200
            assert b"Full text content" in resp.data

    def test_fulltext_not_found(self, seeded_client):
        resp = seeded_client.post("/papers/99999/fulltext")
        assert resp.status_code == 404


class TestFullTextPDFLink:
    """The PDF stays on offer beside text extracted from it.

    Extraction recovers an article's prose but not its figures, tables or
    layout, so a reader who needs those must be able to reach the original.
    """

    def test_pdf_link_shown_beside_extracted_text(self, seeded_client):
        conn = seeded_client.application.config["BMNEWS_DB"]
        paper = get_paper_by_doi(conn, "10.1101/g1")
        with patch("bmnews.gui.routes.papers.FullTextService") as mock_svc:
            mock_svc.return_value.fetch_fulltext.return_value = FullTextResult(
                source="medrxiv",
                html="<p>Extracted body text.</p>",
                pdf_url="https://medrxiv.org/paper.full.pdf",
            )
            resp = seeded_client.post(f"/papers/{paper['id']}/fulltext")

        assert resp.status_code == 200
        assert b"Extracted body text." in resp.data
        assert b"https://medrxiv.org/paper.full.pdf" in resp.data
        assert b"View PDF" in resp.data

    def test_no_pdf_link_without_a_pdf(self, seeded_client):
        """JATS-derived text has no PDF behind it, so no button is offered."""
        conn = seeded_client.application.config["BMNEWS_DB"]
        paper = get_paper_by_doi(conn, "10.1101/g1")
        with patch("bmnews.gui.routes.papers.FullTextService") as mock_svc:
            mock_svc.return_value.fetch_fulltext.return_value = FullTextResult(
                source="europepmc", html="<p>Parsed from JATS.</p>",
            )
            resp = seeded_client.post(f"/papers/{paper['id']}/fulltext")

        assert resp.status_code == 200
        assert b"View PDF" not in resp.data

    def test_pdf_link_survives_caching(self, seeded_client):
        """A second request is served from the DB and must still offer the PDF."""
        conn = seeded_client.application.config["BMNEWS_DB"]
        paper = get_paper_by_doi(conn, "10.1101/g1")
        with patch("bmnews.gui.routes.papers.FullTextService") as mock_svc:
            mock_svc.return_value.fetch_fulltext.return_value = FullTextResult(
                source="medrxiv",
                html="<p>Extracted body text.</p>",
                pdf_url="https://medrxiv.org/paper.full.pdf",
            )
            seeded_client.post(f"/papers/{paper['id']}/fulltext")
            # The service must not be consulted again for the cached paper.
            mock_svc.return_value.fetch_fulltext.reset_mock()
            resp = seeded_client.post(f"/papers/{paper['id']}/fulltext")
            mock_svc.return_value.fetch_fulltext.assert_not_called()

        assert b"https://medrxiv.org/paper.full.pdf" in resp.data
        assert b"View PDF" in resp.data


class TestLauncher:
    def test_find_free_port(self):
        from bmnews.gui.launcher import _find_free_port
        port = _find_free_port()
        assert 1024 < port < 65536

    def test_build_app(self, tmp_path):
        from bmnews.gui.launcher import _build_app
        config = AppConfig()
        config.database.sqlite_path = str(tmp_path / "test.db")
        app, conn = _build_app(config)
        assert app is not None
        assert conn is not None
        conn.close()


class TestGuiCLI:
    def test_gui_command_exists(self):
        from click.testing import CliRunner

        from bmnews.cli import main
        runner = CliRunner()
        result = runner.invoke(main, ["gui", "--help"])
        assert result.exit_code == 0
        assert "Launch" in result.output or "GUI" in result.output


class TestPagination:
    """Regression tests for the infinite-scroll 'Load more' button."""

    @pytest.fixture
    def many_papers_client(self, app):
        conn = app.config["BMNEWS_DB"]
        for i in range(45):
            pid = store_paper(
                conn, doi=f"10.1101/page{i}", title=f"Paged Paper {i}",
                abstract="Kadabra unique term", source="medrxiv",
                published_date="2026-02-10",
            )
            save_score(conn, paper_id=pid, combined_score=0.5)
        return app.test_client()

    def test_first_page_offers_load_more(self, many_papers_client):
        resp = many_papers_client.get("/papers")
        assert resp.status_code == 200
        assert b"load-more" in resp.data

    def test_appended_page_still_offers_load_more(self, many_papers_client):
        """The button used to vanish after one click, capping the list at 40."""
        resp = many_papers_client.get("/papers/more?offset=20&limit=20")
        assert resp.status_code == 200
        assert b"load-more" in resp.data
        assert b"offset=40" in resp.data

    def test_last_page_has_no_load_more(self, many_papers_client):
        resp = many_papers_client.get("/papers/more?offset=40&limit=20")
        assert resp.status_code == 200
        assert b"load-more" not in resp.data

    def test_load_more_carries_search_term(self, many_papers_client):
        """Otherwise page 2 of a search silently returns unfiltered results."""
        resp = many_papers_client.get("/search?q=Kadabra")
        assert resp.status_code == 200
        assert b"q=Kadabra" in resp.data

    def test_more_applies_search_filter(self, many_papers_client):
        conn = many_papers_client.application.config["BMNEWS_DB"]
        pid = store_paper(conn, doi="10.1101/other", title="Unrelated",
                          abstract="nothing to see", source="medrxiv")
        save_score(conn, paper_id=pid, combined_score=0.9)

        resp = many_papers_client.get("/papers/more?offset=0&limit=50&q=Kadabra")
        assert resp.status_code == 200
        assert b"Unrelated" not in resp.data
        assert b"Paged Paper" in resp.data

    def test_negative_offset_is_clamped(self, many_papers_client):
        resp = many_papers_client.get("/papers/more?offset=-5&limit=20")
        assert resp.status_code == 200


class TestSettingsSave:
    def test_invalid_number_reports_error(self, client):
        resp = client.post("/settings/save", data={"sources.lookback_days": "abc"})
        assert resp.status_code == 200
        assert b"Not saved" in resp.data

    def test_valid_values_are_applied(self, client):
        resp = client.post("/settings/save", data={
            "sources.lookback_days": "12",
            "scoring.min_combined": "0.75",
            "llm.provider": "anthropic",
        })
        assert resp.status_code == 200
        assert b"Settings saved" in resp.data
        config = client.application.config["BMNEWS_CONFIG"]
        assert config.sources.lookback_days == 12
        assert config.scoring.min_combined == 0.75
        assert config.llm.provider == "anthropic"

    def test_absent_sources_field_does_not_clear_enabled(self, client):
        """A partial form post must not silently disable every source."""
        config = client.application.config["BMNEWS_CONFIG"]
        config.sources.enabled = ["medrxiv", "europepmc"]
        client.post("/settings/save", data={"sources.lookback_days": "5"})
        assert config.sources.enabled == ["medrxiv", "europepmc"]

    def test_explicit_empty_sources_clears_enabled(self, client):
        """The hidden marker makes an empty selection an intentional clear."""
        config = client.application.config["BMNEWS_CONFIG"]
        config.sources.enabled = ["medrxiv"]
        client.post("/settings/save", data={"sources.enabled_submitted": "1"})
        assert config.sources.enabled == []

    def test_checked_sources_are_applied(self, client):
        config = client.application.config["BMNEWS_CONFIG"]
        client.post("/settings/save", data={
            "sources.enabled_submitted": "1",
            "sources.enabled": ["medrxiv", "pubmed"],
        })
        assert config.sources.enabled == ["medrxiv", "pubmed"]


class TestTemplateEditor:
    def test_unknown_template_is_404(self, client):
        assert client.get("/settings/template/does-not-exist.txt").status_code == 404

    def test_traversal_name_is_rejected(self, client):
        assert client.post("/settings/template/..", data={"content": "x"}).status_code == 404

    def test_known_template_loads(self, client):
        resp = client.get("/settings/template/relevance_system.txt")
        assert resp.status_code == 200
