"""Tests for the GUI Flask app."""

from __future__ import annotations

import pytest
from unittest.mock import patch
from bmlib.db import connect_sqlite
from bmlib.fulltext import FullTextResult
from bmnews.config import AppConfig
from bmnews.db.schema import init_db
from bmnews.db.operations import upsert_paper, save_score, get_paper_by_doi


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
    p1 = upsert_paper(conn, doi="10.1101/g1", title="Alpha Paper",
                       authors="Smith J", abstract="Cancer immunotherapy.",
                       source="medrxiv", published_date="2026-02-10")
    save_score(conn, paper_id=p1, relevance_score=0.9, quality_score=0.8,
               combined_score=0.86, summary="A strong trial.",
               study_design="rct", quality_tier="TIER_4_EXPERIMENTAL")

    p2 = upsert_paper(conn, doi="10.1101/g2", title="Beta Paper",
                       authors="Jones K", abstract="Genomics study.",
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
        assert b"BioMedical News" in resp.data

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
        with patch("bmnews.pipeline.run_pipeline") as mock_run:
            resp = client.post("/pipeline/run")
            assert resp.status_code == 200
            mock_run.assert_called_once()


class TestEndToEnd:
    def test_full_workflow(self, seeded_client):
        resp = seeded_client.get("/")
        assert resp.status_code == 200
        assert b"BioMedical News" in resp.data

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
        with patch("bmnews.gui.routes.papers.FullTextService") as MockSvc:
            instance = MockSvc.return_value
            instance.fetch_fulltext.return_value = FullTextResult(
                source="europepmc", html="<p>Full text content</p>",
            )
            resp = seeded_client.post(f"/papers/{paper['id']}/fulltext")
            assert resp.status_code == 200

    def test_fulltext_returns_html_fragment(self, seeded_client):
        conn = seeded_client.application.config["BMNEWS_DB"]
        paper = get_paper_by_doi(conn, "10.1101/g1")
        with patch("bmnews.gui.routes.papers.FullTextService") as MockSvc:
            instance = MockSvc.return_value
            instance.fetch_fulltext.return_value = FullTextResult(
                source="europepmc", html="<p>Full text content</p>",
            )
            resp = seeded_client.post(f"/papers/{paper['id']}/fulltext")
            assert resp.status_code == 200
            assert b"Full text content" in resp.data

    def test_fulltext_not_found(self, seeded_client):
        resp = seeded_client.post("/papers/99999/fulltext")
        assert resp.status_code == 404


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
