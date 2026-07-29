"""Tests for ``run_notify`` — selection, paging, dispatch and recording.

A file-backed SQLite database and a recording fake adapter: no SMTP, no HTTP,
no LLM. The database is on disk rather than in memory because each run opens
and closes its own connection, and an in-memory database dies with the
connection that made it. The
property these exist to protect is paging. The pending queue is *derived* on
each run rather than stored, so "deliver 5, then 5 more, then find it empty"
must produce no gaps and no repeats — which is exactly what breaks if the
delivery cap is pushed down into the SQL, ahead of the Python matcher.
"""

from __future__ import annotations

import pytest
from bmlib.db import connect_sqlite

from bmnews.config import AppConfig
from bmnews.db.operations import save_paper_tags, save_score, store_paper
from bmnews.db.schema import init_db
from bmnews.notify.channels import ChannelError
from bmnews.notify.service import collect_matches, pending_counts, run_notify
from bmnews.notify.watches import parse_watches


class _RecordingAdapter:
    """Stands in for a channel, remembering what it was asked to deliver."""

    def __init__(self, name: str = "chat", fail: bool = False):
        self.name = name
        self.fail = fail
        self.batches: list[list[str]] = []
        self.txn_keys: list[str] = []

    def send(self, message, *, txn_key: str) -> None:
        self.txn_keys.append(txn_key)
        # The text template numbers its entries ("1. Paper 03"); strip that so
        # assertions compare titles rather than positions.
        titles = [
            line.strip().split(". ", 1)[1]
            for line in message.text.splitlines()
            if line.strip()[:1].isdigit() and ". " in line
        ]
        self.batches.append(titles)
        if self.fail:
            raise ChannelError("delivery refused")

    def delivered_titles(self) -> list[str]:
        return [title for batch in self.batches for title in batch]


@pytest.fixture
def env(monkeypatch, tmp_path):
    """A database, a config with one watch, and a fake adapter behind it.

    File-backed rather than in-memory: ``run_notify`` opens and closes a
    connection per run, and an in-memory database only lives as long as its
    connection does. Handing out a fresh connection per call is also what
    production actually does, so paging across runs is exercised properly.
    """
    db_path = str(tmp_path / "notify.db")
    conn = connect_sqlite(db_path)
    init_db(conn)

    config = AppConfig()
    config.notifications.enabled = True
    config.notifications.channels = {"chat": {"kind": "matrix"}}
    config.notifications.watches = {
        "melanoma": {
            "enabled": True,
            "min_relevance": 0.5,
            "channels": ["chat"],
            "max_per_run": 5,
        }
    }

    adapter = _RecordingAdapter()
    monkeypatch.setattr("bmnews.notify.service.open_db", lambda _config: connect_sqlite(db_path))
    monkeypatch.setattr("bmnews.notify.service.build_adapter", lambda _c, _cfg: adapter)

    yield conn, config, adapter
    conn.close()


def _papers(conn, count: int, *, relevance: float = 0.9, tags=None, prefix: str = "Paper"):
    """Store *count* scored papers, descending in combined score."""
    ids = []
    for n in range(count):
        paper_id = store_paper(conn, doi=f"10.1/{prefix}{n}", title=f"{prefix} {n:02d}")
        save_score(
            conn,
            paper_id=paper_id,
            relevance_score=relevance,
            combined_score=0.99 - n / 1000.0,
            quality_tier="TIER_2_STRONG",
        )
        if tags:
            save_paper_tags(conn, paper_id=paper_id, tags=tags)
        ids.append(paper_id)
    return ids


class TestPaging:
    def test_pages_without_gaps_or_repeats(self, env):
        """Deliver 5, then 5 more, then find the queue empty."""
        conn, config, adapter = env
        _papers(conn, 12)

        first = run_notify(config)
        second = run_notify(config)
        third = run_notify(config)
        fourth = run_notify(config)

        assert [r.delivered for r in first + second + third + fourth] == [5, 5, 2, 0]

        titles = adapter.delivered_titles()
        assert len(titles) == len(set(titles)) == 12, "a paper was skipped or sent twice"
        assert titles == sorted(titles), "paging did not follow the score ordering"
        assert fourth[0].exhausted is True
        assert fourth[0].remaining == 0

    def test_a_batch_filling_at_a_chunk_boundary_is_not_exhaustion(self, env, monkeypatch):
        """Delivering exactly one chunk's worth does not mean the queue is empty.

        The scan window and the delivery cap are different numbers; conflating
        them would report this queue exhausted with five papers still in it.
        """
        monkeypatch.setattr("bmnews.notify.service.NOTIFY_SCAN_CHUNK", 5)
        conn, config, adapter = env
        _papers(conn, 10)

        report = run_notify(config)[0]

        assert report.delivered == 5
        assert report.exhausted is False
        assert report.remaining == 5

    def test_count_overrides_the_watch_cap(self, env):
        conn, config, adapter = env
        _papers(conn, 12)

        report = run_notify(config, count=3)[0]
        assert report.delivered == 3
        assert report.remaining == 9

    def test_drain_delivers_everything(self, env):
        conn, config, adapter = env
        _papers(conn, 12)

        report = run_notify(config, drain=True)[0]
        assert report.delivered == 12
        assert report.remaining == 0
        assert report.exhausted is True

    def test_python_rejections_do_not_shorten_the_batch(self, env, monkeypatch):
        """The matcher rejecting rows must be topped up, not delivered short."""
        monkeypatch.setattr("bmnews.notify.service.NOTIFY_SCAN_CHUNK", 4)
        conn, config, adapter = env
        config.notifications.watches["melanoma"]["tags"] = ["melanoma"]

        # Interleave so any single chunk holds a mix of matching and not.
        for n in range(12):
            paper_id = store_paper(conn, doi=f"10.1/mix{n}", title=f"Paper {n:02d}")
            save_score(conn, paper_id=paper_id, relevance_score=0.9, combined_score=0.99 - n / 100)
            save_paper_tags(
                conn, paper_id=paper_id, tags=["melanoma"] if n % 2 == 0 else ["cardiology"]
            )

        report = run_notify(config)[0]
        assert report.delivered == 5, "a full batch was available and was not assembled"


class TestCollectMatches:
    """The scan itself, exercised directly rather than through a delivery."""

    def _watch(self, config):
        return parse_watches(config.notifications.watches)["melanoma"]

    def test_scans_past_the_chunk_window_to_exhaustion(self, env, monkeypatch):
        """The chunk is a scan window, so a full one is never the end of the queue."""
        monkeypatch.setattr("bmnews.notify.service.NOTIFY_SCAN_CHUNK", 3)
        conn, config, _ = env
        _papers(conn, 10)

        assert len(collect_matches(conn, self._watch(config), "chat")) == 10

    def test_a_queue_that_ends_exactly_on_a_chunk_boundary(self, env, monkeypatch):
        """The empty top-up chunk is what ends the scan, not a short one."""
        monkeypatch.setattr("bmnews.notify.service.NOTIFY_SCAN_CHUNK", 5)
        conn, config, _ = env
        _papers(conn, 10)

        assert len(collect_matches(conn, self._watch(config), "chat")) == 10

    def test_an_empty_queue_scans_clean(self, env):
        conn, config, _ = env

        assert collect_matches(conn, self._watch(config), "chat") == []

    def test_does_not_carry_the_gui_fulltext_cache(self, env):
        """The scan walks the whole queue, so it must not select cached articles.

        Every candidate is materialised at once; pulling `fulltext_html` along
        would put the entire cached corpus in memory to answer a question that
        reads none of it.
        """
        conn, config, _ = env
        _papers(conn, 1)

        paper = collect_matches(conn, self._watch(config), "chat")[0]
        assert "fulltext_html" not in paper

        # Still everything the matcher tests and the templates render.
        for key in ("id", "title", "abstract", "journal", "sources", "url", "quality_tier"):
            assert key in paper, f"{key} is needed downstream and was dropped"


class TestDedupAndRetry:
    def test_a_second_run_with_nothing_new_delivers_nothing(self, env):
        conn, config, adapter = env
        _papers(conn, 3)

        assert run_notify(config)[0].delivered == 3
        assert run_notify(config)[0].delivered == 0
        assert len(adapter.batches) == 1, "an empty batch was still dispatched"

    def test_a_failed_delivery_is_retried_and_then_sticks(self, env, monkeypatch):
        conn, config, _ = env
        _papers(conn, 2)

        failing = _RecordingAdapter(fail=True)
        monkeypatch.setattr("bmnews.notify.service.build_adapter", lambda _c, _cfg: failing)
        report = run_notify(config)[0]
        assert (report.delivered, report.failed) == (0, 2)
        assert report.remaining == 2, "a failed paper must stay in the queue"

        working = _RecordingAdapter()
        monkeypatch.setattr("bmnews.notify.service.build_adapter", lambda _c, _cfg: working)
        retried = run_notify(config)[0]
        assert (retried.delivered, retried.failed) == (2, 0)
        assert run_notify(config)[0].delivered == 0

    def test_one_channel_failing_leaves_the_other_delivered(self, env, monkeypatch):
        conn, config, _ = env
        config.notifications.channels["mail"] = {"kind": "email"}
        config.notifications.watches["melanoma"]["channels"] = ["chat", "mail"]
        _papers(conn, 2)

        good, bad = _RecordingAdapter("chat"), _RecordingAdapter("mail", fail=True)
        adapters = {"chat": good, "mail": bad}
        monkeypatch.setattr(
            "bmnews.notify.service.build_adapter", lambda channel, _cfg: adapters[channel.name]
        )

        by_channel = {r.channel: r for r in run_notify(config)}
        assert by_channel["chat"].delivered == 2
        assert by_channel["mail"].failed == 2

        # Only the channel that failed retries.
        again = {r.channel: r for r in run_notify(config)}
        assert again["chat"].delivered == 0
        assert again["mail"].delivered == 0  # still failing
        assert again["mail"].failed == 2

    def test_a_repeated_channel_name_delivers_one_batch(self, env):
        """A repeat used to send twice — once per resolved copy of the channel.

        The second pass re-derives the queue, so it sent the *next* batch to the
        same destination rather than the same one, silently doubling
        ``max_per_run`` for that watch.
        """
        conn, config, adapter = env
        config.notifications.watches["melanoma"]["channels"] = ["chat", "chat"]
        _papers(conn, 12)

        reports = run_notify(config)

        assert len(reports) == 1
        assert reports[0].delivered == 5
        assert len(adapter.batches) == 1


class TestCriteria:
    def test_criteria_are_applied(self, env):
        conn, config, adapter = env
        config.notifications.watches["melanoma"]["keywords"] = ["melanoma"]

        wanted = store_paper(conn, doi="10.1/hit", title="Paper melanoma trial")
        save_score(conn, paper_id=wanted, relevance_score=0.9, combined_score=0.9)
        missed = store_paper(conn, doi="10.1/miss", title="Paper about cardiology")
        save_score(conn, paper_id=missed, relevance_score=0.9, combined_score=0.95)

        assert run_notify(config)[0].delivered == 1
        assert adapter.delivered_titles() == ["Paper melanoma trial"]

    def test_score_floor_excludes(self, env):
        conn, config, adapter = env
        config.notifications.watches["melanoma"]["min_relevance"] = 0.8
        _papers(conn, 2, relevance=0.4)
        _papers(conn, 1, relevance=0.9, prefix="Good")

        assert run_notify(config)[0].delivered == 1

    def test_a_disabled_watch_is_not_run(self, env):
        conn, config, adapter = env
        config.notifications.watches["melanoma"]["enabled"] = False
        _papers(conn, 3)

        assert run_notify(config) == []
        assert adapter.batches == []

    def test_disabled_notifications_deliver_nothing(self, env):
        conn, config, adapter = env
        config.notifications.enabled = False
        _papers(conn, 3)

        assert run_notify(config) == []

    def test_watch_selects_one_by_name(self, env):
        conn, config, adapter = env
        config.notifications.watches["other"] = {"channels": ["chat"]}
        _papers(conn, 2)

        reports = run_notify(config, watch="melanoma")
        assert [r.watch for r in reports] == ["melanoma"]


class TestDryRun:
    def test_records_nothing_and_sends_nothing(self, env):
        conn, config, adapter = env
        _papers(conn, 3)

        report = run_notify(config, dry_run=True)[0]
        assert report.delivered == 3
        assert adapter.batches == []
        # Nothing was recorded, so a real run still has all three to send.
        assert run_notify(config)[0].delivered == 3

    def test_the_running_total_does_not_move(self, env):
        """`delivered` answers "what would go"; `sent_total` must stay a fact."""
        conn, config, _ = env
        _papers(conn, 3)

        report = run_notify(config, dry_run=True)[0]

        assert report.dry_run is True
        assert report.delivered == 3
        assert report.sent_total == 0, "a rehearsal must not claim papers were delivered"


class TestPendingCounts:
    def test_reports_delivered_matching_and_remaining(self, env):
        conn, config, adapter = env
        _papers(conn, 8)
        run_notify(config)  # delivers 5

        report = pending_counts(config)[0]
        assert (report.sent_total, report.remaining) == (5, 3)
        assert report.matching == 8
        # `delivered` is what *this* call sent, and pending_counts sends nothing.
        assert report.delivered == 0

    def test_includes_disabled_watches(self, env):
        conn, config, adapter = env
        config.notifications.watches["melanoma"]["enabled"] = False
        _papers(conn, 4)

        report = pending_counts(config)[0]
        assert report.remaining == 4
        assert report.enabled is False

    def test_a_failed_paper_still_counts_as_remaining(self, env, monkeypatch):
        """Only `sent` dequeues, so a failed row must still read as queued."""
        conn, config, _ = env
        _papers(conn, 3)
        monkeypatch.setattr(
            "bmnews.notify.service.build_adapter", lambda _c, _cfg: _RecordingAdapter(fail=True)
        )
        run_notify(config)

        report = pending_counts(config)[0]
        assert (report.sent_total, report.remaining) == (0, 3)
        assert report.exhausted is False

    def test_a_repeated_channel_is_reported_once(self, env):
        """Two reports for one pair render as two identical rows in the pane."""
        conn, config, _ = env
        config.notifications.watches["melanoma"]["channels"] = ["chat", "chat"]
        _papers(conn, 2)

        assert [(r.watch, r.channel) for r in pending_counts(config)] == [("melanoma", "chat")]


class TestCLI:
    """The `bmnews notify` command, over a stubbed service layer."""

    def _run(self, monkeypatch, argv, *, reports=None, calls=None):
        from click.testing import CliRunner

        from bmnews.cli import main

        def _record(name):
            def _fake(config, **kwargs):
                if calls is not None:
                    calls.append((name, kwargs))
                return reports if reports is not None else []

            return _fake

        monkeypatch.setattr("bmnews.notify.service.run_notify", _record("run"))
        monkeypatch.setattr("bmnews.notify.service.pending_counts", _record("list"))
        return CliRunner().invoke(main, ["notify", *argv])

    def test_reports_what_was_delivered(self, monkeypatch):
        from bmnews.notify.service import DeliveryReport

        report = DeliveryReport(
            watch="melanoma", channel="chat", delivered=3, remaining=7, matching=10
        )
        result = self._run(monkeypatch, [], reports=[report])

        assert result.exit_code == 0
        assert "melanoma" in result.output
        assert "3" in result.output and "7" in result.output

    def test_says_so_when_there_is_nothing_to_send(self, monkeypatch):
        result = self._run(monkeypatch, [], reports=[])
        assert result.exit_code == 0
        assert "nothing" in result.output.lower()

    def test_list_does_not_deliver(self, monkeypatch):
        calls = []
        result = self._run(monkeypatch, ["--list"], calls=calls)

        assert result.exit_code == 0
        assert [name for name, _ in calls] == ["list"]

    def test_flags_reach_the_service(self, monkeypatch):
        calls = []
        self._run(monkeypatch, ["--watch", "melanoma", "--count", "3"], calls=calls)
        assert calls[0][1] == {"watch": "melanoma", "count": 3, "drain": False, "dry_run": False}

        calls.clear()
        self._run(monkeypatch, ["--all", "--dry-run"], calls=calls)
        assert calls[0][1] == {"watch": "", "count": None, "drain": True, "dry_run": True}

    def test_failures_are_reported_and_exit_nonzero(self, monkeypatch):
        from bmnews.notify.service import DeliveryReport

        report = DeliveryReport(watch="melanoma", channel="mail", failed=2)
        result = self._run(monkeypatch, [], reports=[report])

        assert "fail" in result.output.lower()
        assert result.exit_code == 1, "a run whose deliveries all failed must not look successful"

    @pytest.mark.parametrize(
        "argv,expected",
        [
            (["--all", "--count", "3"], "one or the other"),
            (["--count", "0"], "at least 1"),
            (["--count", "-2"], "at least 1"),
        ],
    )
    def test_contradictory_batch_sizes_are_refused(self, monkeypatch, argv, expected):
        """Silently picking one of two conflicting answers delivers a number nobody asked for."""
        calls = []
        result = self._run(monkeypatch, argv, calls=calls)

        assert result.exit_code != 0
        assert expected in result.output
        assert calls == [], "nothing should have been delivered"
