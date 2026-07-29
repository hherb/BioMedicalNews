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
