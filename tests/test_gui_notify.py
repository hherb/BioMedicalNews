"""Tests for the GUI watches pane."""

from __future__ import annotations

import pytest
from bmlib.db import connect_sqlite

from bmnews.config import AppConfig
from bmnews.db.schema import init_db
from bmnews.gui import jobs
from bmnews.notify.service import DeliveryReport


@pytest.fixture(autouse=True)
def idle_jobs():
    """Leave the module-level job state clean around every test."""
    jobs.wait_for_idle(5.0)
    jobs.status().update(running=False, message="Ready", status="idle", refresh_list=False)
    yield
    jobs.wait_for_idle(5.0)
    jobs.status().update(running=False, message="Ready", status="idle", refresh_list=False)


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
        assert "mailbox" in body
        assert ">3<" in body  # delivered
        assert ">12<" in body  # matching
        assert ">9<" in body  # remaining

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

    def test_a_second_delivery_is_refused_while_one_runs(self, client, monkeypatch):
        import threading

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

    def test_an_unknown_watch_is_a_404(self, client, monkeypatch):
        monkeypatch.setattr(
            "bmnews.notify.service.run_notify",
            lambda config, **kwargs: pytest.fail("must not be called"),
        )

        assert client.post("/watches/nosuchwatch/notify").status_code == 404
        assert client.post("/watches/nosuchwatch/notify-all").status_code == 404

    def test_the_response_attaches_the_completion_poller(self, client, monkeypatch):
        monkeypatch.setattr("bmnews.notify.service.run_notify", lambda config, **kwargs: [])

        body = client.post("/watches/melanoma/notify").data.decode()

        assert jobs.wait_for_idle(5.0) is True
        assert 'id="watch-poller"' in body
        assert 'hx-swap-oob="innerHTML"' in body
        assert 'hx-get="/watches/rows"' in body
