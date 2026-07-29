"""Tests for the GUI's shared background job."""

from __future__ import annotations

import threading

import pytest

from bmnews.gui import jobs


@pytest.fixture(autouse=True)
def idle_jobs():
    """Leave the module-level job state clean around every test."""
    jobs.wait_for_idle(5.0)
    jobs.status().update(running=False, message="Ready", status="idle", refresh_list=False)
    yield
    jobs.wait_for_idle(5.0)
    jobs.status().update(running=False, message="Ready", status="idle", refresh_list=False)


class TestStart:
    def test_runs_the_target_and_reports_busy(self):
        ran = threading.Event()

        def _target() -> None:
            ran.set()
            jobs.status().update(running=False, message="Done.", status="success")

        assert jobs.start(message="Working...", target=_target, error_label="Job error") is True
        assert jobs.wait_for_idle(5.0) is True
        assert ran.is_set()
        assert jobs.status()["message"] == "Done."
        assert jobs.status()["status"] == "success"

    def test_second_job_is_refused_while_one_runs(self):
        release = threading.Event()
        started = threading.Event()
        runs = []

        def _target() -> None:
            runs.append(1)
            started.set()
            release.wait(5.0)
            jobs.status().update(running=False, message="Done.", status="success")

        assert jobs.start(message="First...", target=_target, error_label="Job error") is True
        started.wait(5.0)
        assert jobs.start(message="Second...", target=_target, error_label="Job error") is False
        # The refusal must not overwrite the running job's own progress line.
        assert jobs.status()["message"] == "First..."
        release.set()
        assert jobs.wait_for_idle(5.0) is True
        assert runs == [1]

    def test_a_raising_target_publishes_an_error_and_frees_the_lock(self):
        def _boom() -> None:
            raise RuntimeError("no homeserver")

        assert jobs.start(message="Working...", target=_boom, error_label="Job error") is True
        assert jobs.wait_for_idle(5.0) is True
        assert jobs.status()["status"] == "error"
        assert "Job error: no homeserver" == jobs.status()["message"]
        # The lock was released, so the next job can start.
        assert jobs.start(message="Next...", target=lambda: None, error_label="Job error") is True
        assert jobs.wait_for_idle(5.0) is True

    def test_a_target_that_forgets_to_clear_running_is_corrected(self):
        assert (
            jobs.start(message="Working...", target=lambda: None, error_label="Job error") is True
        )
        assert jobs.wait_for_idle(5.0) is True
        assert jobs.running() is False


class TestProgress:
    def test_progress_replaces_the_message(self):
        jobs.progress("Scoring 3 papers...")
        assert jobs.status()["message"] == "Scoring 3 papers..."
