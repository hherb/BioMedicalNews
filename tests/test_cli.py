"""The CLI itself, rather than any one command.

Every command shares one failure path: an exception nobody anticipated has to
reach the user as a message rather than a traceback, and it must not flatten
the exit codes commands set deliberately.
"""

from __future__ import annotations

import logging
import sqlite3

import click
import pytest
from click.testing import CliRunner

from bmnews.config import AppConfig


def _boom(*args, **kwargs):
    """Fail the way a busy database does, rather than with a toy exception."""
    raise sqlite3.OperationalError("database is locked")


def _invoke(monkeypatch, args, config=None):
    """Run the real CLI against a config of our own, whatever is on disk.

    ``main()``'s group callback calls ``load_config()`` and overwrites
    ``ctx.obj["config"]``, so patching that is what actually threads a config
    through — passing ``obj=`` alone is not enough.
    """
    from bmnews.cli import main

    config = config or AppConfig()
    monkeypatch.setattr("bmnews.cli.load_config", lambda path: config)
    return CliRunner().invoke(main, args, obj={"config": config})


class TestAnUnexpectedFailure:
    """What a command raising something nobody planned for looks like."""

    def test_it_reports_a_message_rather_than_a_traceback(self, monkeypatch):
        monkeypatch.setattr("bmnews.pipeline.run_score", _boom)

        result = _invoke(monkeypatch, ["score"])

        assert result.exit_code == 1
        assert "Error: score failed: OperationalError: database is locked" in result.output
        assert "Traceback" not in result.output

    def test_the_traceback_survives_in_the_log(self, monkeypatch, caplog):
        """The message is for the user; the traceback is what a bug report
        needs, so it is logged rather than discarded — ``bmnews -v`` prints
        it."""
        monkeypatch.setattr("bmnews.pipeline.run_score", _boom)

        with caplog.at_level(logging.DEBUG, logger="bmnews.cli"):
            _invoke(monkeypatch, ["score"])

        assert any(record.exc_info for record in caplog.records)

    @pytest.mark.parametrize(
        ("args", "target"),
        [
            (["run"], "bmnews.pipeline.run_pipeline"),
            (["fetch"], "bmnews.pipeline.run_sync"),
            (["score"], "bmnews.pipeline.run_score"),
            (["digest"], "bmnews.pipeline.run_digest"),
            (["notify"], "bmnews.notify.service.run_notify"),
            (["transparency"], "bmnews.transparency.service.run_transparency"),
            (["search", "cancer"], "bmnews.db.operations.get_papers_filtered"),
        ],
    )
    def test_every_command_is_covered(self, monkeypatch, tmp_path, args, target):
        """Handled once for the whole group, not per command.

        The point of doing it at the group is that a command added later
        cannot forget it, so this asserts across the commands rather than on
        the one that prompted the change.
        """
        monkeypatch.setattr(target, _boom)
        config = AppConfig()
        config.database.sqlite_path = str(tmp_path / "bmnews.db")
        config.transparency.enabled = True

        result = _invoke(monkeypatch, args, config=config)

        assert result.exit_code == 1
        assert f"Error: {args[0]} failed:" in result.output


class TestWhatTheWrapperLeavesAlone:
    """Deliberate exits have to survive it.

    ``click.exceptions.Exit`` and ``click.Abort`` are both ``RuntimeError``
    subclasses, so a bare ``except Exception`` swallows them — which would turn
    ``notify``'s "every delivery failed" exit into a spurious error message and
    lose the exit code a cron job reads.
    """

    def _run(self, body):
        """Invoke a one-off command in a group built like the real one."""
        from bmnews.cli import _CleanFailureGroup

        @click.group(cls=_CleanFailureGroup)
        def group():
            pass

        @group.command("thing")
        @click.pass_context
        def thing(ctx):
            body(ctx)

        return CliRunner().invoke(group, ["thing"])

    def test_a_commands_own_exit_code_survives(self):
        result = self._run(lambda ctx: ctx.exit(3))

        assert result.exit_code == 3
        assert "Error" not in result.output

    def test_a_usage_error_still_exits_two(self):
        def _body(ctx):
            raise click.UsageError("--all and --count both set the batch size")

        result = self._run(_body)

        assert result.exit_code == 2
        assert "--all and --count" in result.output

    def test_an_abort_is_still_an_abort(self):
        def _body(ctx):
            raise click.Abort()

        result = self._run(_body)

        assert "Aborted" in result.output
        assert "Error:" not in result.output

    def test_an_exception_carrying_no_message_still_names_itself(self):
        """``str(exc)`` is empty for a bare raise, and "Error: thing failed: "
        tells the user nothing at all."""

        def _body(ctx):
            raise RuntimeError()

        result = self._run(_body)

        assert "Error: thing failed: RuntimeError" in result.output
