#!/usr/bin/env python3

"""Tests for the multi-process job scheduler.

The fake job targets below must stay module-level functions: the scheduler uses
the "spawn" start method, so a target has to be importable by qualified name in
the child process.
"""

import logging
import queue
import time
from pathlib import Path
from typing import Any

import pytest

from bdfr.configuration import Configuration
from bdfr.jobs import EventKind, Job, JobEvent, JobManager, JobState, _QueueLogHandler


def fake_job_success(job_id: str, config: Configuration, event_queue: Any) -> None:
    """Report two completed submissions and finish cleanly."""
    event_queue.put(JobEvent(job_id=job_id, kind=EventKind.STATE, state=JobState.RUNNING))
    for index in (1, 2):
        event_queue.put(JobEvent(job_id=job_id, kind=EventKind.PROGRESS, message=f"item {index}", completed=index))
    event_queue.put(
        JobEvent(job_id=job_id, kind=EventKind.STATE, state=JobState.COMPLETED, message="Finished", completed=2)
    )


def fake_job_failure(job_id: str, config: Configuration, event_queue: Any) -> None:
    event_queue.put(JobEvent(job_id=job_id, kind=EventKind.STATE, state=JobState.RUNNING))
    event_queue.put(
        JobEvent(
            job_id=job_id,
            kind=EventKind.STATE,
            state=JobState.FAILED,
            message="RuntimeError: boom",
            level=logging.ERROR,
        )
    )
    raise SystemExit(1)


def fake_job_crash(job_id: str, config: Configuration, event_queue: Any) -> None:
    """Die without reporting any terminal state."""
    event_queue.put(JobEvent(job_id=job_id, kind=EventKind.STATE, state=JobState.RUNNING))
    raise SystemExit(3)


def fake_job_touch_file(job_id: str, config: Configuration, event_queue: Any) -> None:
    """Record that this job ran, so parallelism can be observed from the parent."""
    marker = Path(config.directory) / f"{job_id}.started"
    marker.write_text("running", encoding="utf-8")
    event_queue.put(JobEvent(job_id=job_id, kind=EventKind.STATE, state=JobState.RUNNING))
    # Stay alive long enough for the parent to inspect concurrency.
    time.sleep(3)
    event_queue.put(JobEvent(job_id=job_id, kind=EventKind.STATE, state=JobState.COMPLETED))


def fake_job_forever(job_id: str, config: Configuration, event_queue: Any) -> None:
    event_queue.put(JobEvent(job_id=job_id, kind=EventKind.STATE, state=JobState.RUNNING))
    time.sleep(600)


def _config_for(tmp_path: Path) -> Configuration:
    config = Configuration()
    config.directory = str(tmp_path)
    config.time_format = "ISO"
    return config


def _pump_until(manager: JobManager, predicate, timeout: float = 90.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        manager.pump()
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_job_state_terminality():
    assert JobState.COMPLETED.is_terminal
    assert JobState.FAILED.is_terminal
    assert JobState.CANCELLED.is_terminal
    assert not JobState.PENDING.is_terminal
    assert not JobState.RUNNING.is_terminal


def test_add_job_preserves_order(tmp_path: Path):
    manager = JobManager(max_parallel=1, job_target=fake_job_success)
    labels = ["alice", "bob", "carol"]
    for label in labels:
        manager.add_job(label, _config_for(tmp_path))
    assert [job.label for job in manager.ordered_jobs] == labels


def test_job_ids_are_unique(tmp_path: Path):
    manager = JobManager(job_target=fake_job_success)
    ids = {manager.add_job(f"user{i}", _config_for(tmp_path)).job_id for i in range(20)}
    assert len(ids) == 20


@pytest.mark.slow
def test_successful_job_reports_progress_and_completes(tmp_path: Path):
    manager = JobManager(max_parallel=2, job_target=fake_job_success)
    job = manager.add_job("alice", _config_for(tmp_path))
    try:
        assert _pump_until(manager, lambda: job.state.is_terminal), job.last_message
        assert job.state is JobState.COMPLETED
        assert job.completed == 2
    finally:
        manager.shutdown()


@pytest.mark.slow
def test_failed_job_is_marked_failed(tmp_path: Path):
    manager = JobManager(max_parallel=2, job_target=fake_job_failure)
    job = manager.add_job("alice", _config_for(tmp_path))
    try:
        assert _pump_until(manager, lambda: job.state.is_terminal), job.last_message
        assert job.state is JobState.FAILED
        assert "boom" in job.last_message
    finally:
        manager.shutdown()


@pytest.mark.slow
def test_silent_crash_is_detected_from_exit_code(tmp_path: Path):
    """A process that dies without reporting must not be left marked running."""
    manager = JobManager(max_parallel=2, job_target=fake_job_crash)
    job = manager.add_job("alice", _config_for(tmp_path))
    try:
        assert _pump_until(manager, lambda: job.state.is_terminal), job.last_message
        assert job.state is JobState.FAILED
        assert "code 3" in job.last_message
    finally:
        manager.shutdown()


@pytest.mark.slow
def test_all_jobs_complete_across_several_batches(tmp_path: Path):
    manager = JobManager(max_parallel=2, job_target=fake_job_success)
    jobs = [manager.add_job(f"user{i}", _config_for(tmp_path)) for i in range(5)]
    try:
        assert _pump_until(manager, lambda: manager.is_finished), [j.state for j in jobs]
        assert all(job.state is JobState.COMPLETED for job in jobs)
        assert all(job.completed == 2 for job in jobs)
    finally:
        manager.shutdown()


@pytest.mark.slow
def test_max_parallel_is_respected(tmp_path: Path):
    """Never run more jobs at once than the configured limit."""
    limit = 2
    manager = JobManager(max_parallel=limit, job_target=fake_job_touch_file)
    for i in range(5):
        manager.add_job(f"user{i}", _config_for(tmp_path))
    peak = 0
    try:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline and not manager.is_finished:
            manager.pump()
            peak = max(peak, manager.running_count)
            assert manager.running_count <= limit, f"{manager.running_count} jobs ran at once, limit is {limit}"
            time.sleep(0.05)
        assert manager.is_finished, [j.state for j in manager.ordered_jobs]
        assert peak == limit, f"never reached the limit; peak was {peak}"
        # Every job really did run in its own process.
        started = list(tmp_path.glob("*.started"))
        assert len(started) == 5, started
    finally:
        manager.shutdown()


@pytest.mark.slow
def test_pending_job_can_be_cancelled_before_it_starts(tmp_path: Path):
    manager = JobManager(max_parallel=1, job_target=fake_job_forever)
    first = manager.add_job("first", _config_for(tmp_path))
    second = manager.add_job("second", _config_for(tmp_path))
    try:
        assert _pump_until(manager, lambda: first.state is JobState.RUNNING)
        assert second.state is JobState.PENDING
        assert manager.cancel(second.job_id) is True
        assert second.state is JobState.CANCELLED
        manager.pump()
        # Cancelling a queued job must not start it.
        assert second.process is None
    finally:
        manager.shutdown()


@pytest.mark.slow
def test_running_job_can_be_cancelled(tmp_path: Path):
    manager = JobManager(max_parallel=1, job_target=fake_job_forever)
    job = manager.add_job("alice", _config_for(tmp_path))
    try:
        assert _pump_until(manager, lambda: job.state is JobState.RUNNING)
        assert manager.cancel(job.job_id) is True
        assert job.state is JobState.CANCELLED
        assert _pump_until(manager, lambda: job.process is None, timeout=60)
        # A cancel must stick, not be overwritten by a late event from the child.
        assert job.state is JobState.CANCELLED
    finally:
        manager.shutdown()


@pytest.mark.slow
def test_cancel_frees_a_slot_for_the_next_job(tmp_path: Path):
    manager = JobManager(max_parallel=1, job_target=fake_job_forever)
    first = manager.add_job("first", _config_for(tmp_path))
    second = manager.add_job("second", _config_for(tmp_path))
    try:
        assert _pump_until(manager, lambda: first.state is JobState.RUNNING)
        manager.cancel(first.job_id)
        assert _pump_until(manager, lambda: second.state is JobState.RUNNING, timeout=60)
    finally:
        manager.shutdown()


def test_cancel_unknown_job_is_false():
    manager = JobManager(job_target=fake_job_success)
    assert manager.cancel("does-not-exist") is False


@pytest.mark.slow
def test_shutdown_terminates_everything(tmp_path: Path):
    manager = JobManager(max_parallel=3, job_target=fake_job_forever)
    jobs = [manager.add_job(f"user{i}", _config_for(tmp_path)) for i in range(3)]
    assert _pump_until(manager, lambda: manager.running_count == 3)
    processes = [job.process for job in jobs]
    manager.shutdown()
    assert all(job.process is None for job in jobs)
    for process in processes:
        assert process is not None
        assert not process.is_alive()


@pytest.mark.slow
def test_shutdown_is_idempotent(tmp_path: Path):
    manager = JobManager(max_parallel=1, job_target=fake_job_success)
    manager.add_job("alice", _config_for(tmp_path))
    manager.pump()
    manager.shutdown()
    manager.shutdown()


def test_is_finished_on_empty_manager():
    manager = JobManager(job_target=fake_job_success)
    assert manager.is_finished


def test_max_parallel_floor_is_one():
    assert JobManager(max_parallel=0, job_target=fake_job_success).max_parallel == 1
    assert JobManager(max_parallel=-5, job_target=fake_job_success).max_parallel == 1


def test_job_config_survives_the_spawn_boundary(tmp_path: Path):
    """Configuration must pickle, or jobs cannot start on Windows."""
    import pickle

    config = _config_for(tmp_path)
    config.user = ["someone"]
    config.skip = ["gif", "avi"]
    config.concurrency = 6
    restored = pickle.loads(pickle.dumps(config))
    assert restored.user == ["someone"]
    assert restored.skip == ["gif", "avi"]
    assert restored.concurrency == 6
    assert restored.directory == str(tmp_path)


def _log_record(message: str, **extra) -> logging.LogRecord:
    record = logging.LogRecord("bdfr.downloader", logging.INFO, __file__, 1, message, None, None)
    record.__dict__.update(extra)
    return record


def test_run_summary_is_kept_in_the_final_message():
    """Otherwise "Finished" replaces the reason a re-run downloaded nothing."""
    events = queue.Queue()
    handler = _QueueLogHandler("job1", events)
    assert handler.completion_message == "Finished"

    summary = "Skipped 3 posts already downloaded (use --recheck to verify them again)"
    handler.emit(_log_record(summary, bdfr_event="run_summary"))
    handler.emit(_log_record("Program complete"))

    assert handler.completion_message == f"Finished. {summary}"
    # The summary is still shown as it happens, like any other log line.
    assert events.get_nowait().message == summary


def test_every_run_summary_is_kept_in_the_final_message():
    """A listing that gave up must not be hidden by the skip summary logged after it."""
    handler = _QueueLogHandler("job1", queue.Queue())
    incomplete = "Giving up on submitted posts of u/alice after 5 failed attempts in a row. The listing is INCOMPLETE"
    skipped = "Skipped 3 posts already downloaded (use --recheck to verify them again)"

    handler.emit(_log_record(incomplete, bdfr_event="run_summary"))
    handler.emit(_log_record("Download complete: 2 submissions processed, 0 filtered out, 0 failed"))
    handler.emit(_log_record(skipped, bdfr_event="run_summary"))

    assert handler.completion_message == f"Finished. {incomplete} {skipped}"


def test_job_dataclass_defaults(tmp_path: Path):
    job = Job(label="alice", config=_config_for(tmp_path))
    assert job.state is JobState.PENDING
    assert job.completed == 0
    assert job.is_active is False
    assert len(job.job_id) == 8
