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

    def test_a_config_that_will_not_load_is_reported_the_same_way(self, tmp_path):
        """``load_config`` runs in the group callback, which the wrapper covers too.

        Click sets ``invoked_subcommand`` before invoking the group callback,
        so the message still names the command the user typed.
        """
        bad = tmp_path / "bad.toml"
        bad.write_text("this is not = valid toml [[[\n")

        from bmnews.cli import main

        result = CliRunner().invoke(main, ["-c", str(bad), "score"])

        assert result.exit_code == 1
        assert "Error: score failed: TOMLDecodeError" in result.output
        assert "Traceback" not in result.output

    def test_the_traceback_survives_a_failure_before_the_config_loads(self, monkeypatch, tmp_path):
        """``-v`` must configure logging *before* ``load_config`` can raise.

        Configuring it afterwards left the root logger at WARNING for exactly
        the failure whose message tells the user to re-run with ``-v``: the
        DEBUG record was dropped and the traceback went nowhere, so the advice
        the message gives was false.

        Run against the real handler, not ``caplog`` — ``caplog`` installs its
        own and would pass the record whichever order the code used, which is
        the whole thing under test. The root logger is emptied first because
        ``basicConfig`` is a no-op once any handler is installed, and pytest
        has installed several.
        """
        bad = tmp_path / "bad.toml"
        bad.write_text("this is not = valid toml [[[\n")
        root = logging.getLogger()
        monkeypatch.setattr(root, "handlers", [])
        monkeypatch.setattr(root, "level", logging.WARNING)

        from bmnews.cli import main

        result = CliRunner().invoke(main, ["-v", "-c", str(bad), "score"])

        assert "Traceback" in result.stderr
        assert "TOMLDecodeError" in result.stderr

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
            (["init"], "bmnews.cli.write_default_config"),
            (["gui"], "bmnews.gui.launcher.launch"),
        ],
    )
    def test_every_command_is_covered(self, monkeypatch, tmp_path, args, target):
        """Handled once for the whole group, not per command.

        The point of doing it at the group is that a command added later
        cannot forget it, so this asserts across the commands rather than on
        the one that prompted the change. All nine are listed anyway: an
        exception is cheaper than the argument about which of them "counts",
        and a command dropping off this list is then a visible deletion.
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

    ``click.exceptions.Exit`` and ``click.Abort`` derive from ``RuntimeError``
    and ``ClickException`` from ``Exception`` directly, so a bare
    ``except Exception`` swallows all three — which would turn ``notify``'s
    "every delivery failed" exit into a spurious error message and lose the
    exit code a cron job reads. Narrowing the passthrough to ``RuntimeError``
    would keep two of the three and quietly lose ``ClickException``, so the
    usage-error case below is doing real work rather than restating click.
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
