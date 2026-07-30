"""Tests for the transparency stage.

``TransparencyAnalyzer.analyze`` is always mocked: the real one makes four to
eight requests to CrossRef, Europe PMC, PubMed, OpenAlex and
ClinicalTrials.gov per paper.
"""

from __future__ import annotations

import json

import pytest
from bmlib.transparency import TransparencyResult, TransparencyRisk

from bmnews.config import AppConfig
from bmnews.db.operations import (
    get_transparency_results,
    save_score,
    save_transparency,
    store_paper,
)
from bmnews.db.schema import init_db
from bmnews.transparency import service


def _result(paper_id, *, score=80, risk=TransparencyRisk.LOW, indicators=()):
    """Build a bmlib result the way the analyzer would return one."""
    return TransparencyResult(
        document_id=str(paper_id),
        transparency_score=score,
        risk_level=risk,
        risk_indicators=list(indicators),
    )


class _FakeAnalyzer:
    """Stands in for bmlib's analyzer, recording what it was asked."""

    def __init__(self, results=None, *, raises=()):
        self.results = results or {}
        self.raises = set(raises)
        self.calls = []

    def analyze(self, document_id, *, pmid=None, doi=None):
        self.calls.append((document_id, pmid, doi))
        if document_id in self.raises:
            raise RuntimeError("CrossRef exploded")
        return self.results.get(document_id) or _result(document_id)


@pytest.fixture
def db(tmp_path):
    """A migrated file-backed database the service will reopen for itself."""
    path = tmp_path / "bmnews.db"
    config = AppConfig()
    config.database.sqlite_path = str(path)
    config.transparency.enabled = True
    config.transparency.min_combined_score = 0.5

    from bmnews.db.schema import open_db

    conn = open_db(config)
    init_db(conn)
    yield config, conn
    conn.close()


def _scored(conn, *, doi, combined, pmid=None):
    paper_id = store_paper(
        conn, doi=doi, pmid=pmid, title=f"Paper {doi}", abstract="Abstract", source="medrxiv"
    )
    save_score(conn, paper_id=paper_id, relevance_score=0.9, combined_score=combined)
    return paper_id


def _install(monkeypatch, analyzer):
    monkeypatch.setattr(service, "TransparencyAnalyzer", lambda **kwargs: analyzer)


class TestRunTransparency:
    def test_disabled_config_builds_no_analyzer(self, db, monkeypatch):
        """bmlib answers a disabled analyze() with an UNKNOWN placeholder, and
        storing one would satisfy the 'no row yet' half of the candidate query
        — so the paper would never be analysed once the feature was enabled."""
        config, conn = db
        config.transparency.enabled = False
        _scored(conn, doi="10.1/a", combined=0.9)

        def _boom(**kwargs):
            raise AssertionError("analyzer must not be constructed when disabled")

        monkeypatch.setattr(service, "TransparencyAnalyzer", _boom)

        report = service.run_transparency(config)

        assert report == service.TransparencyReport()
        assert get_transparency_results(conn) == []

    def test_analyses_and_stores_a_candidate(self, db, monkeypatch):
        config, conn = db
        paper_id = _scored(conn, doi="10.1/a", combined=0.9)
        analyzer = _FakeAnalyzer({str(paper_id): _result(paper_id, score=82)})
        _install(monkeypatch, analyzer)

        report = service.run_transparency(config)

        assert report.analyzed == 1
        assert report.indeterminate == 0
        rows = get_transparency_results(conn)
        assert rows[0]["transparency_score"] == 82
        assert rows[0]["risk_level"] == "low"
        assert json.loads(rows[0]["result_json"])["transparency_score"] == 82

    def test_passes_both_identifiers_to_the_analyzer(self, db, monkeypatch):
        config, conn = db
        paper_id = _scored(conn, doi="10.1/a", pmid="99", combined=0.9)
        analyzer = _FakeAnalyzer()
        _install(monkeypatch, analyzer)

        service.run_transparency(config)

        assert analyzer.calls == [(str(paper_id), "99", "10.1/a")]

    def test_skips_papers_below_the_gate(self, db, monkeypatch):
        config, conn = db
        _scored(conn, doi="10.1/low", combined=0.1)
        analyzer = _FakeAnalyzer()
        _install(monkeypatch, analyzer)

        report = service.run_transparency(config)

        assert report.analyzed == 0
        assert analyzer.calls == []

    def test_dry_run_counts_without_analysing(self, db, monkeypatch):
        config, conn = db
        _scored(conn, doi="10.1/a", combined=0.9)

        def _boom(**kwargs):
            raise AssertionError("dry run must not construct an analyzer")

        monkeypatch.setattr(service, "TransparencyAnalyzer", _boom)

        report = service.run_transparency(config, dry_run=True)

        assert report.candidates == 1
        assert report.analyzed == 0
        assert get_transparency_results(conn) == []

    def test_a_raising_analysis_costs_only_itself(self, db, monkeypatch):
        config, conn = db
        bad = _scored(conn, doi="10.1/bad", combined=0.9)
        good = _scored(conn, doi="10.1/good", combined=0.8)
        analyzer = _FakeAnalyzer(raises=[str(bad)])
        _install(monkeypatch, analyzer)

        report = service.run_transparency(config)

        assert report.failed == 1
        assert report.analyzed == 1
        assert [r["paper_id"] for r in get_transparency_results(conn)] == [good]

    def test_a_failed_analysis_leaves_no_row_so_it_retries(self, db, monkeypatch):
        config, conn = db
        bad = _scored(conn, doi="10.1/bad", combined=0.9)
        _install(monkeypatch, _FakeAnalyzer(raises=[str(bad)]))

        service.run_transparency(config)

        assert get_transparency_results(conn) == []

    def test_unknown_result_is_reported_indeterminate(self, db, monkeypatch):
        config, conn = db
        paper_id = _scored(conn, doi="10.1/a", combined=0.9)
        analyzer = _FakeAnalyzer(
            {str(paper_id): _result(paper_id, score=0, risk=TransparencyRisk.UNKNOWN)}
        )
        _install(monkeypatch, analyzer)

        report = service.run_transparency(config)

        assert (report.analyzed, report.indeterminate, report.exhausted) == (1, 1, 0)

    def test_reaching_the_attempt_ceiling_is_reported_exhausted(self, db, monkeypatch):
        """The only outcome the user cannot fix by waiting, so it is counted."""
        config, conn = db
        paper_id = _scored(conn, doi="10.1/a", combined=0.9)
        analyzer = _FakeAnalyzer(
            {str(paper_id): _result(paper_id, score=0, risk=TransparencyRisk.UNKNOWN)}
        )
        _install(monkeypatch, analyzer)

        for _ in range(3):
            report = service.run_transparency(config)

        assert report.exhausted == 1
        assert service.run_transparency(config).analyzed == 0, "queue is spent"

    def test_refresh_reanalyses_and_resets_the_budget(self, db, monkeypatch):
        config, conn = db
        paper_id = _scored(conn, doi="10.1/a", combined=0.9)
        save_transparency(conn, paper_id=paper_id, transparency_score=0, risk_level="unknown")
        save_transparency(conn, paper_id=paper_id, transparency_score=0, risk_level="unknown")
        save_transparency(conn, paper_id=paper_id, transparency_score=0, risk_level="unknown")
        assert service.run_transparency(config).analyzed == 0

        _install(monkeypatch, _FakeAnalyzer({str(paper_id): _result(paper_id, score=70)}))
        report = service.run_transparency(config, refresh=True)

        assert report.analyzed == 1
        rows = get_transparency_results(conn)
        assert rows[0]["attempts"] == 1
        assert rows[0]["risk_level"] == "low"

    def test_paper_id_analyses_below_the_gate(self, db, monkeypatch):
        config, conn = db
        low = _scored(conn, doi="10.1/low", combined=0.01)
        analyzer = _FakeAnalyzer()
        _install(monkeypatch, analyzer)

        report = service.run_transparency(config, paper_id=low)

        assert report.analyzed == 1
        assert analyzer.calls == [(str(low), None, "10.1/low")]

    def test_paper_id_with_refresh_redoes_one_determinate_paper(self, db, monkeypatch):
        """The documented way to redo a single paper: --paper-id --refresh.
        Alone, --paper-id selects nothing once a determinate result exists."""
        config, conn = db
        paper_id = _scored(conn, doi="10.1/a", combined=0.9)
        save_transparency(conn, paper_id=paper_id, transparency_score=82, risk_level="low")
        _install(monkeypatch, _FakeAnalyzer())

        assert service.run_transparency(config, paper_id=paper_id).analyzed == 0

        report = service.run_transparency(config, paper_id=paper_id, refresh=True)

        assert report.analyzed == 1
        assert get_transparency_results(conn)[0]["attempts"] == 1

    def test_limit_caps_the_batch(self, db, monkeypatch):
        config, conn = db
        for i in range(4):
            _scored(conn, doi=f"10.1/{i}", combined=0.9 - i * 0.01)
        analyzer = _FakeAnalyzer()
        _install(monkeypatch, analyzer)

        report = service.run_transparency(config, limit=2)

        assert report.analyzed == 2
        assert len(analyzer.calls) == 2

    def test_a_full_batch_warns_that_more_remain(self, db, monkeypatch, caplog):
        config, conn = db
        for i in range(3):
            _scored(conn, doi=f"10.1/{i}", combined=0.9)
        _install(monkeypatch, _FakeAnalyzer())

        with caplog.at_level("WARNING"):
            service.run_transparency(config, limit=3)

        assert "more" in caplog.text.lower()

    def test_progress_is_reported(self, db, monkeypatch):
        config, conn = db
        _scored(conn, doi="10.1/a", combined=0.9)
        _install(monkeypatch, _FakeAnalyzer())
        messages = []

        service.run_transparency(config, on_progress=messages.append)

        assert any("ransparency" in m for m in messages)

    def test_concurrency_greater_than_one_stores_every_result(self, db, monkeypatch):
        """Storage happens on the calling thread; a worker must never touch
        the connection."""
        config, conn = db
        config.transparency.concurrency = 4
        for i in range(6):
            _scored(conn, doi=f"10.1/{i}", combined=0.9)
        _install(monkeypatch, _FakeAnalyzer())

        report = service.run_transparency(config)

        assert report.analyzed == 6
        assert len(get_transparency_results(conn)) == 6


class TestBuildSettings:
    def test_enabled_is_forced_true(self, db):
        config, _ = db
        assert service.build_settings(config).enabled is True

    def test_score_threshold_and_concurrency_are_passed_through(self, db):
        config, _ = db
        config.transparency.score_threshold = 55
        config.transparency.concurrency = 7

        settings = service.build_settings(config)

        assert settings.score_threshold == 55
        assert settings.max_concurrent_analyses == 7

    def test_downgrade_flags_keep_bmlib_defaults(self, db):
        """They feed calculate_risk_level, not only the tier downgrade this
        stage ignores — so they shape the badge we display."""
        config, _ = db
        settings = service.build_settings(config)

        assert settings.industry_funding_triggers_downgrade is True
        assert settings.missing_coi_triggers_downgrade is True

    def test_filtering_stays_off(self, db):
        """Caller-honoured, and this caller does not filter."""
        config, _ = db
        assert service.build_settings(config).filtering_enabled is False


class TestListResults:
    def test_lists_stored_results(self, db):
        config, conn = db
        paper_id = _scored(conn, doi="10.1/a", combined=0.9)
        save_transparency(conn, paper_id=paper_id, transparency_score=20, risk_level="high")

        rows = service.list_results(config)

        assert rows[0]["risk_level"] == "high"
        assert rows[0]["title"] == "Paper 10.1/a"
