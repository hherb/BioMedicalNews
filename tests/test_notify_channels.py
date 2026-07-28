"""Tests for the notification channel adapters and message rendering.

Mocked SMTP and a fake HTTP client throughout — no email leaves the machine and
no homeserver is contacted, matching the patterns the digest and fetcher suites
already use.
"""

from __future__ import annotations

from urllib.parse import quote

import pytest

from bmnews.config import AppConfig
from bmnews.notify.channels import (
    ChannelError,
    Message,
    build_adapter,
    transaction_key,
)
from bmnews.notify.channels.matrix import MatrixChannel
from bmnews.notify.watches import Channel

ROOM_ID = "!abcdef:example.org"
ROOM_ALIAS = "#bmnews-alerts:example.org"


def _message() -> Message:
    return Message(subject="melanoma-trials: 2 new papers", html="<p>Two</p>", text="Two")


def _config(**email_overrides) -> AppConfig:
    config = AppConfig()
    config.email.smtp_host = "smtp.example.org"
    config.email.from_address = "bmnews@example.org"
    config.email.to_address = "me@example.org"
    for key, value in email_overrides.items():
        setattr(config.email, key, value)
    return config


# --- Fake HTTP --------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = str(self._payload)

    def json(self) -> dict:
        return self._payload


class _FakeHTTP:
    """Stands in for an ``httpx.Client``, recording what was asked of it."""

    def __init__(self, *, encrypted: bool = False, put_status: int = 200):
        self.encrypted = encrypted
        self.put_status = put_status
        self.gets: list[str] = []
        self.puts: list[tuple[str, dict, dict]] = []

    def get(self, url: str, headers: dict | None = None) -> _FakeResponse:
        self.gets.append(url)
        if "/directory/room/" in url:
            return _FakeResponse(200, {"room_id": ROOM_ID})
        if url.endswith("/state/m.room.encryption"):
            if self.encrypted:
                return _FakeResponse(200, {"algorithm": "m.megolm.v1.aes-sha2"})
            return _FakeResponse(404, {"errcode": "M_NOT_FOUND"})
        raise AssertionError(f"unexpected GET {url}")

    def put(self, url: str, headers: dict | None = None, json: dict | None = None):
        self.puts.append((url, headers or {}, json or {}))
        if self.put_status == 200:
            return _FakeResponse(200, {"event_id": "$evt:example.org"})
        return _FakeResponse(self.put_status, {"errcode": "M_FORBIDDEN", "error": "not invited"})


def _matrix(http: _FakeHTTP, room: str = ROOM_ID) -> MatrixChannel:
    return MatrixChannel(
        name="chat",
        homeserver="https://matrix.example.org",
        access_token="syt_secret",
        room=room,
        client=http,
    )


# --- Matrix -----------------------------------------------------------------


class TestMatrixDelivery:
    def test_puts_to_the_send_endpoint(self):
        http = _FakeHTTP()
        _matrix(http).send(_message(), txn_key="abc123")

        url, headers, body = http.puts[0]
        assert url == (
            "https://matrix.example.org/_matrix/client/v3/rooms/"
            f"{quote(ROOM_ID, safe='')}/send/m.room.message/abc123"
        )
        assert headers["Authorization"] == "Bearer syt_secret"
        assert body["msgtype"] == "m.text"
        assert body["body"] == "Two"
        assert body["format"] == "org.matrix.custom.html"
        assert body["formatted_body"] == "<p>Two</p>"

    def test_transaction_id_comes_from_the_caller(self):
        """The txnId is the homeserver's idempotency key, so it must not be random."""
        http = _FakeHTTP()
        channel = _matrix(http)
        channel.send(_message(), txn_key="stable-key")
        channel.send(_message(), txn_key="stable-key")

        assert http.puts[0][0] == http.puts[1][0]

    def test_resolves_an_alias_once(self):
        http = _FakeHTTP()
        channel = _matrix(http, room=ROOM_ALIAS)
        channel.send(_message(), txn_key="one")
        channel.send(_message(), txn_key="two")

        directory_calls = [url for url in http.gets if "/directory/room/" in url]
        assert len(directory_calls) == 1
        assert quote(ROOM_ALIAS, safe="") in directory_calls[0]
        assert all(quote(ROOM_ID, safe="") in url for url, _, _ in http.puts)

    def test_refuses_an_encrypted_room(self):
        """Posting to an E2EE room would put ciphertext nobody can read and report success."""
        http = _FakeHTTP(encrypted=True)
        with pytest.raises(ChannelError, match="encrypted"):
            _matrix(http).send(_message(), txn_key="k")
        assert http.puts == []

    def test_raises_on_http_error(self):
        http = _FakeHTTP(put_status=403)
        with pytest.raises(ChannelError, match="403"):
            _matrix(http).send(_message(), txn_key="k")


class TestTransactionKey:
    def test_is_stable_across_calls(self):
        assert transaction_key("w", "chat", [3, 1, 2]) == transaction_key("w", "chat", [3, 1, 2])

    def test_ignores_paper_order(self):
        assert transaction_key("w", "chat", [1, 2, 3]) == transaction_key("w", "chat", [3, 2, 1])

    def test_differs_by_papers_watch_and_channel(self):
        base = transaction_key("w", "chat", [1, 2])
        assert base != transaction_key("w", "chat", [1, 2, 3])
        assert base != transaction_key("other", "chat", [1, 2])
        assert base != transaction_key("w", "mail", [1, 2])

    def test_is_url_safe(self):
        key = transaction_key("watch/with slashes", "chat", [1])
        assert quote(key, safe="") == key


# --- Email ------------------------------------------------------------------


class TestEmailDelivery:
    def test_sends_through_the_shared_smtp_helper(self, monkeypatch):
        sent = {}

        def _fake_send(**kwargs):
            sent.update(kwargs)
            return True

        monkeypatch.setattr("bmnews.notify.channels.email.send_email", _fake_send)
        adapter = build_adapter(Channel(name="mail", kind="email"), _config())
        adapter.send(_message(), txn_key="k")

        assert sent["to_address"] == "me@example.org"
        assert sent["smtp_host"] == "smtp.example.org"
        assert sent["html_body"] == "<p>Two</p>"
        assert sent["text_body"] == "Two"
        assert sent["subject"] == "[BioMedNews] melanoma-trials: 2 new papers"

    def test_failure_raises_rather_than_returning_false(self, monkeypatch):
        """A send that did not happen must not be recorded as delivered."""
        monkeypatch.setattr("bmnews.notify.channels.email.send_email", lambda **_: False)
        adapter = build_adapter(Channel(name="mail", kind="email"), _config())

        with pytest.raises(ChannelError):
            adapter.send(_message(), txn_key="k")

    def test_channel_overrides_the_recipient_and_prefix(self, monkeypatch):
        sent = {}
        monkeypatch.setattr(
            "bmnews.notify.channels.email.send_email",
            lambda **kwargs: sent.update(kwargs) or True,
        )
        channel = Channel(
            name="mail",
            kind="email",
            settings={"to_address": "alerts@example.org", "subject_prefix": "[ALERT]"},
        )
        build_adapter(channel, _config()).send(_message(), txn_key="k")

        assert sent["to_address"] == "alerts@example.org"
        assert sent["subject"] == "[ALERT] melanoma-trials: 2 new papers"

    def test_recipient_falls_back_to_the_user_address(self, monkeypatch):
        sent = {}
        monkeypatch.setattr(
            "bmnews.notify.channels.email.send_email",
            lambda **kwargs: sent.update(kwargs) or True,
        )
        config = _config(to_address="")
        config.user.email = "fallback@example.org"
        build_adapter(Channel(name="mail", kind="email"), config).send(_message(), txn_key="k")

        assert sent["to_address"] == "fallback@example.org"


class TestBuildAdapter:
    def test_email_without_an_smtp_host_is_refused(self):
        with pytest.raises(ChannelError, match="smtp_host"):
            build_adapter(Channel(name="mail", kind="email"), _config(smtp_host=""))

    def test_email_without_a_recipient_is_refused(self):
        config = _config(to_address="")
        config.user.email = ""
        with pytest.raises(ChannelError, match="recipient"):
            build_adapter(Channel(name="mail", kind="email"), config)

    def test_matrix_needs_homeserver_token_and_room(self):
        settings = {"homeserver": "https://matrix.example.org", "access_token": "t", "room": "!r:s"}
        for missing in settings:
            partial = {key: value for key, value in settings.items() if key != missing}
            with pytest.raises(ChannelError, match=missing):
                build_adapter(Channel(name="chat", kind="matrix", settings=partial), _config())

    def test_unknown_kind_is_refused(self):
        with pytest.raises(ChannelError, match="carrier-pigeon"):
            build_adapter(Channel(name="x", kind="carrier-pigeon"), _config())


# --- Rendering --------------------------------------------------------------


class TestRendering:
    """The four notification templates, rendered through the real engine."""

    def _papers(self):
        return [
            {
                "title": "Adjuvant immunotherapy in melanoma",
                "url": "https://doi.org/10.1101/one",
                "authors": ["Smith J", "Doe A"],
                "publication_date": "2026-07-20",
                "sources": ["medrxiv"],
                "summary": "Checkpoint blockade improved survival.",
                "relevance_score": 0.91,
                "quality_tier": "TIER_4_EXPERIMENTAL",
                "study_design": "rct",
            },
            {
                "title": "A review of adjuvant strategies",
                "url": "https://doi.org/10.1101/two",
                "authors": ["Brown B"],
                "publication_date": "2026-07-21",
                "sources": ["europepmc"],
                "summary": "Narrative overview.",
                "relevance_score": 0.75,
                "quality_tier": "TIER_5_SYNTHESIS",
                "study_design": "review",
            },
        ]

    def _render(self, medium, fmt, **kwargs):
        from pathlib import Path

        from bmlib.templates import TemplateEngine

        from bmnews.notify.renderer import render_notification

        engine = TemplateEngine(default_dir=Path(__file__).parent.parent / "templates")
        return render_notification(
            self._papers(),
            watch_name=kwargs.pop("watch_name", "melanoma-trials"),
            templates=engine,
            medium=medium,
            fmt=fmt,
            **kwargs,
        )

    @pytest.mark.parametrize("medium", ["email", "matrix"])
    @pytest.mark.parametrize("fmt", ["html", "text"])
    def test_every_paper_and_link_appears(self, medium, fmt):
        out = self._render(medium, fmt)
        assert "Adjuvant immunotherapy in melanoma" in out
        assert "A review of adjuvant strategies" in out
        assert "https://doi.org/10.1101/one" in out

    @pytest.mark.parametrize("fmt", ["html", "text"])
    def test_the_watch_is_named(self, fmt):
        assert "melanoma-trials" in self._render("email", fmt)

    def test_matrix_html_carries_no_css(self):
        """Matrix's HTML subset has no CSS support, and clients sanitise differently."""
        out = self._render("matrix", "html")
        assert "<style" not in out
        assert "style=" not in out
        assert "<table" not in out

    def test_text_rendering_has_no_markup(self):
        out = self._render("email", "text")
        assert "<p" not in out and "<div" not in out

    def test_remaining_is_reported_only_when_some_are_left(self):
        assert "3" in self._render("email", "text", remaining=3)
        assert "remaining" in self._render("email", "text", remaining=3).lower()
        assert "remaining" not in self._render("email", "text", remaining=0).lower()
