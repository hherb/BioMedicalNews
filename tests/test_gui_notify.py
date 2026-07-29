"""Tests for the GUI watches pane."""

from __future__ import annotations

import re
import threading
from unittest.mock import Mock

import pytest
from bmlib.db import connect_sqlite

from bmnews.config import AppConfig
from bmnews.db.schema import init_db
from bmnews.gui import jobs
from bmnews.notify.service import DeliveryReport

# The autouse ``idle_jobs`` fixture that resets jobs' process state lives in
# tests/conftest.py, so every suite touching the GUI gets it.


def channel_row(name: str, delivered: int, matching: int, remaining: int) -> re.Pattern[str]:
    """A pattern matching one channel row *in column order*.

    Substring assertions on the numbers alone pass just as happily when
    delivered, matching and remaining are rendered in the wrong columns, which
    is the one way this table can lie.
    """
    cells = (name, delivered, matching, remaining)
    return re.compile(r"\s*".join(f"<td>{cell}</td>" for cell in cells))


@pytest.fixture
def config(tmp_path):
    """A config with one watch on one channel.

    ``sqlite_path`` is pointed at a scratch file so that a route reaching the
    database despite the patches below cannot touch the developer's own.
    """
    config = AppConfig()
    config.database.sqlite_path = str(tmp_path / "test.db")
    config.notifications.enabled = True
    config.notifications.channels = {
        "mailbox": {"kind": "email", "to_address": "reader@example.com"},
    }
    config.notifications.watches = {
        "melanoma": {
            "min_relevance": 0.7,
            "tags": ["melanoma"],
            "channels": ["mailbox"],
            "max_per_run": 5,
        },
    }
    return config


@pytest.fixture
def client(config):
    from bmnews.gui.app import create_app

    conn = connect_sqlite(":memory:")
    init_db(conn)
    app = create_app(config, conn)
    app.config["TESTING"] = True
    return app.test_client()


def report(watch="melanoma", channel="mailbox", **kwargs):
    """A pending_counts-shaped report, with the fields that function fills."""
    defaults = {"enabled": True, "sent_total": 3, "matching": 12, "remaining": 9}
    return DeliveryReport(watch=watch, channel=channel, **{**defaults, **kwargs})


class TestPane:
    def test_renders_a_row_per_watch_and_channel(self, client, monkeypatch):
        monkeypatch.setattr("bmnews.notify.service.pending_counts", lambda config: [report()])

        resp = client.get("/watches")

        assert resp.status_code == 200
        body = resp.data.decode()
        assert "melanoma" in body
        # channel, delivered, matching, remaining — in that order.
        assert channel_row("mailbox", 3, 12, 9).search(body)

    def test_shows_the_criteria_summary(self, client, monkeypatch):
        monkeypatch.setattr("bmnews.notify.service.pending_counts", lambda config: [report()])

        body = client.get("/watches").data.decode()

        assert "relevance ≥ 0.7" in body
        assert "tags: melanoma" in body

    def test_a_watch_whose_channels_resolve_to_nothing_is_still_listed(
        self, client, config, monkeypatch
    ):
        # resolve_channels() skips an unknown channel name, so pending_counts
        # returns nothing at all for this watch. It must not vanish from the
        # pane — nothing will ever be delivered for it and that is worth saying.
        config.notifications.watches["orphan"] = {"channels": ["nowhere"]}
        monkeypatch.setattr("bmnews.notify.service.pending_counts", lambda config: [report()])

        body = client.get("/watches").data.decode()

        assert "orphan" in body
        assert "no configured channel" in body

    def test_a_watch_whose_channels_resolve_only_partly_names_the_dropped_ones(
        self, client, config, monkeypatch
    ):
        # resolve_channels() logs the bad name and returns the rest, so this
        # watch renders one healthy-looking row and "typo" vanishes. That is
        # the exact failure the pane exists to prevent.
        config.notifications.watches["melanoma"]["channels"] = ["mailbox", "typo"]
        monkeypatch.setattr("bmnews.notify.service.pending_counts", lambda config: [report()])

        body = client.get("/watches").data.decode()

        assert channel_row("mailbox", 3, 12, 9).search(body)
        assert "typo" in body
        assert "nothing will be delivered there" in body

    def test_a_watch_with_no_criteria_says_it_matches_everything(self, client, config, monkeypatch):
        config.notifications.watches = {"everything": {"channels": ["mailbox"]}}
        monkeypatch.setattr(
            "bmnews.notify.service.pending_counts", lambda config: [report(watch="everything")]
        )

        body = client.get("/watches").data.decode()

        assert "no criteria — matches every scored paper" in body

    def test_an_unparseable_watch_is_named(self, client, config, monkeypatch):
        # parse_watches() skips this one with an ERROR log; without the diff
        # against the raw config it would be invisible in the GUI.
        config.notifications.watches["broken"] = {"min_relevance": "very"}
        monkeypatch.setattr("bmnews.notify.service.pending_counts", lambda config: [report()])

        body = client.get("/watches").data.decode()

        assert "broken" in body
        assert "could not be read" in body

    def test_a_disabled_watch_shows_counts_but_no_buttons(self, client, config, monkeypatch):
        config.notifications.watches["melanoma"]["enabled"] = False
        monkeypatch.setattr(
            "bmnews.notify.service.pending_counts", lambda config: [report(enabled=False)]
        )

        body = client.get("/watches").data.decode()

        assert "disabled" in body
        assert ">9<" in body
        assert "/watches/melanoma/notify" not in body

    def test_globally_disabled_notifications_show_a_notice_and_no_buttons(
        self, client, config, monkeypatch
    ):
        config.notifications.enabled = False
        monkeypatch.setattr("bmnews.notify.service.pending_counts", lambda config: [report()])

        body = client.get("/watches").data.decode()

        assert "switched off" in body
        assert "/watches/melanoma/notify" not in body

    def test_an_exhausted_watch_has_no_buttons(self, client, monkeypatch):
        monkeypatch.setattr(
            "bmnews.notify.service.pending_counts",
            lambda config: [report(remaining=0, exhausted=True)],
        )

        body = client.get("/watches").data.decode()

        assert "melanoma" in body
        assert "/watches/melanoma/notify" not in body

    def test_no_watches_configured(self, client, config, monkeypatch):
        config.notifications.watches = {}
        monkeypatch.setattr("bmnews.notify.service.pending_counts", lambda config: [])

        body = client.get("/watches").data.decode()

        assert "No watches configured" in body

    def test_watches_that_all_fail_to_parse_do_not_read_as_none_configured(
        self, client, config, monkeypatch
    ):
        # Telling the user to add a watch when they have added two, and both
        # are broken, sends them to do the thing they already did.
        config.notifications.watches = {"a": {"min_relevance": "very"}, "b": {"max_per_run": 0}}
        monkeypatch.setattr("bmnews.notify.service.pending_counts", lambda config: [])

        page = client.get("/watches").data.decode()
        rows = client.get("/watches/rows").data.decode()

        for body in (page, rows):
            assert "No watches configured" not in body
            assert "No readable watches" in body
        assert "could not be read" in page

    def test_the_tab_is_in_the_shell(self, client):
        body = client.get("/").data.decode()
        assert 'hx-get="/watches"' in body


class TestDelivery:
    def test_notify_starts_a_batch_run(self, client, monkeypatch):
        calls = []

        def _run_notify(config, **kwargs):
            calls.append(kwargs)
            return [DeliveryReport(watch="melanoma", channel="mailbox", delivered=5, remaining=4)]

        monkeypatch.setattr("bmnews.notify.service.run_notify", _run_notify)

        resp = client.post("/watches/melanoma/notify")

        assert resp.status_code == 200
        assert jobs.wait_for_idle(5.0) is True
        assert calls == [{"watch": "melanoma", "drain": False, "on_progress": jobs.progress}]
        assert jobs.status()["status"] == "success"
        assert "5 paper(s) notified" in jobs.status()["message"]

    def test_notify_all_drains(self, client, monkeypatch):
        calls = []

        def _run_notify(config, **kwargs):
            calls.append(kwargs["drain"])
            return [DeliveryReport(watch="melanoma", channel="mailbox", delivered=9)]

        monkeypatch.setattr("bmnews.notify.service.run_notify", _run_notify)

        client.post("/watches/melanoma/notify-all")

        assert jobs.wait_for_idle(5.0) is True
        assert calls == [True]

    def test_a_failed_delivery_reports_as_an_error(self, client, monkeypatch):
        monkeypatch.setattr(
            "bmnews.notify.service.run_notify",
            lambda config, **kwargs: [
                DeliveryReport(watch="melanoma", channel="mailbox", failed=5, remaining=9)
            ],
        )

        client.post("/watches/melanoma/notify")

        assert jobs.wait_for_idle(5.0) is True
        assert jobs.status()["status"] == "error"
        assert "stay queued" in jobs.status()["message"]

    def test_a_partial_failure_reports_both(self, client, monkeypatch):
        monkeypatch.setattr(
            "bmnews.notify.service.run_notify",
            lambda config, **kwargs: [
                DeliveryReport(watch="melanoma", channel="mailbox", delivered=5),
                DeliveryReport(watch="melanoma", channel="chatroom", failed=5),
            ],
        )

        client.post("/watches/melanoma/notify")

        assert jobs.wait_for_idle(5.0) is True
        assert jobs.status()["status"] == "error"
        assert "5 paper(s) notified" in jobs.status()["message"]
        assert "5 failed" in jobs.status()["message"]

    def test_a_raising_run_notify_reports_and_frees_the_lock(self, client, monkeypatch):
        def _boom(config, **kwargs):
            raise RuntimeError("smtp down")

        monkeypatch.setattr("bmnews.notify.service.run_notify", _boom)

        client.post("/watches/melanoma/notify")

        assert jobs.wait_for_idle(5.0) is True
        assert jobs.status()["status"] == "error"
        assert "smtp down" in jobs.status()["message"]
        assert jobs.running() is False

    def test_an_empty_run_reports_nothing_to_notify(self, client, monkeypatch):
        monkeypatch.setattr("bmnews.notify.service.run_notify", lambda config, **kwargs: [])

        client.post("/watches/melanoma/notify")

        assert jobs.wait_for_idle(5.0) is True
        assert jobs.status()["message"] == "melanoma: nothing to notify"
        assert jobs.status()["status"] == "success"

    def test_a_second_delivery_is_refused_while_one_runs(self, client, monkeypatch):
        release = threading.Event()
        started = threading.Event()
        runs = []

        def _slow(config, **kwargs):
            runs.append(kwargs["watch"])
            started.set()
            release.wait(5.0)
            return []

        monkeypatch.setattr("bmnews.notify.service.run_notify", _slow)

        client.post("/watches/melanoma/notify")
        started.wait(5.0)
        resp = client.post("/watches/melanoma/notify")

        assert resp.status_code == 200
        release.set()
        assert jobs.wait_for_idle(5.0) is True
        assert runs == ["melanoma"]

    def test_a_refused_delivery_says_it_did_not_start(self, client, monkeypatch):
        # The delivery buttons stay on screen during a pipeline run, so this is
        # the state a real click lands in. Without a notice the response is the
        # *blocking* job's progress line and the click looks like it worked.
        run_notify = Mock()
        monkeypatch.setattr("bmnews.notify.service.run_notify", run_notify)

        release = threading.Event()
        started = threading.Event()

        def _blocker() -> None:
            started.set()
            release.wait(5.0)

        assert jobs.start(message="Busy...", target=_blocker, error_label="Job error") is True
        started.wait(5.0)
        try:
            body = client.post("/watches/melanoma/notify").data.decode()
        finally:
            release.set()

        assert jobs.wait_for_idle(5.0) is True
        assert "this delivery did not start" in body
        # The poller still goes out: the counts need refreshing when the
        # blocking job ends.
        assert 'hx-get="/watches/rows"' in body
        run_notify.assert_not_called()

    def test_a_started_delivery_clears_the_refusal_slot(self, client, monkeypatch):
        monkeypatch.setattr("bmnews.notify.service.run_notify", lambda config, **kwargs: [])

        body = client.post("/watches/melanoma/notify").data.decode()

        assert jobs.wait_for_idle(5.0) is True
        assert '<div id="watch-message" hx-swap-oob="innerHTML"></div>' in body

    def test_an_unknown_watch_is_a_404(self, client, monkeypatch):
        run_notify = Mock()
        monkeypatch.setattr("bmnews.notify.service.run_notify", run_notify)

        assert client.post("/watches/nosuchwatch/notify").status_code == 404
        assert client.post("/watches/nosuchwatch/notify-all").status_code == 404
        # Both run on the request thread, so unlike a pytest.fail() inside the
        # patched callable they cannot be swallowed by jobs.py's broad except.
        run_notify.assert_not_called()
        assert jobs.running() is False

    def test_the_response_attaches_the_completion_poller(self, client, monkeypatch):
        monkeypatch.setattr("bmnews.notify.service.run_notify", lambda config, **kwargs: [])

        body = client.post("/watches/melanoma/notify").data.decode()

        assert jobs.wait_for_idle(5.0) is True
        assert 'id="watch-poller"' in body
        assert 'hx-swap-oob="innerHTML"' in body
        assert 'hx-get="/watches/rows"' in body


class TestRefresh:
    def test_204_while_a_job_runs_and_no_scan_is_performed(self, client, monkeypatch):
        scans = []

        def _counts(config):
            scans.append(1)
            return [report()]

        monkeypatch.setattr("bmnews.notify.service.pending_counts", _counts)
        jobs.status()["running"] = True

        resp = client.get("/watches/rows")

        assert resp.status_code == 204
        assert resp.data == b""
        # The scan a refresh costs is the thing the 204 exists to avoid.
        assert scans == []

    def test_idle_returns_rows_and_retires_the_poller(self, client, monkeypatch):
        monkeypatch.setattr(
            "bmnews.notify.service.pending_counts", lambda config: [report(remaining=4)]
        )

        resp = client.get("/watches/rows")
        body = resp.data.decode()

        assert resp.status_code == 200
        assert "melanoma" in body
        assert channel_row("mailbox", 3, 12, 4).search(body)
        # The poller's slot is emptied, which is what stops the polling.
        assert '<div id="watch-poller" hx-swap-oob="innerHTML"></div>' in body
        assert 'hx-get="/watches/rows"' not in body

    def test_idle_also_clears_the_stale_refusal_notice(self, client, monkeypatch):
        # #watch-message sits outside #watch-list, so a stale "did not start"
        # notice from an earlier refused click would otherwise survive this
        # refresh and keep telling the user to retry a delivery that would now
        # succeed.
        monkeypatch.setattr(
            "bmnews.notify.service.pending_counts", lambda config: [report(remaining=4)]
        )

        resp = client.get("/watches/rows")
        body = resp.data.decode()

        assert resp.status_code == 200
        assert '<div id="watch-message" hx-swap-oob="innerHTML"></div>' in body

    def test_the_pane_carries_the_poller_when_opened_during_a_run(self, client, monkeypatch):
        monkeypatch.setattr("bmnews.notify.service.pending_counts", lambda config: [report()])
        jobs.status()["running"] = True

        body = client.get("/watches").data.decode()

        assert 'hx-get="/watches/rows"' in body
