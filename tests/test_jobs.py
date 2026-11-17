#!/usr/bin/env python3

"""Tests for the multi-process job scheduler.

The fake job targets below must stay module-level functions: the scheduler uses
the "spawn" start method, so a target has to be importable by qualified name in
the child process.
"""

import logging
import queue
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from bdfr.configuration import Configuration
from bdfr.constants import DEFAULT_CONCURRENCY, MAX_CONCURRENCY, MAX_PARALLEL_JOBS
from bdfr.jobs import EventKind, Job, JobEvent, JobManager, JobState, _QueueLogHandler, run_job


def fake_job_success(job_id: str, config: Configuration, event_queue: Any, live_concurrency: Any = None) -> None:
    """Report two completed submissions and finish cleanly."""
    event_queue.put(JobEvent(job_id=job_id, kind=EventKind.STATE, state=JobState.RUNNING))
    for index in (1, 2):
        event_queue.put(JobEvent(job_id=job_id, kind=EventKind.PROGRESS, message=f"item {index}", completed=index))
    event_queue.put(
        JobEvent(job_id=job_id, kind=EventKind.STATE, state=JobState.COMPLETED, message="Finished", completed=2)
    )


def fake_job_failure(job_id: str, config: Configuration, event_queue: Any, live_concurrency: Any = None) -> None:
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


def fake_job_crash(job_id: str, config: Configuration, event_queue: Any, live_concurrency: Any = None) -> None:
    """Die without reporting any terminal state."""
    event_queue.put(JobEvent(job_id=job_id, kind=EventKind.STATE, state=JobState.RUNNING))
    raise SystemExit(3)


def fake_job_touch_file(job_id: str, config: Configuration, event_queue: Any, live_concurrency: Any = None) -> None:
    """Record that this job ran, so parallelism can be observed from the parent."""
    marker = Path(config.directory) / f"{job_id}.started"
    marker.write_text("running", encoding="utf-8")
    event_queue.put(JobEvent(job_id=job_id, kind=EventKind.STATE, state=JobState.RUNNING))
    # Stay alive long enough for the parent to inspect concurrency.
    time.sleep(3)
    event_queue.put(JobEvent(job_id=job_id, kind=EventKind.STATE, state=JobState.COMPLETED))


def fake_job_forever(job_id: str, config: Configuration, event_queue: Any, live_concurrency: Any = None) -> None:
    event_queue.put(JobEvent(job_id=job_id, kind=EventKind.STATE, state=JobState.RUNNING))
    time.sleep(600)


def fake_job_report_concurrency(
    job_id: str, config: Configuration, event_queue: Any, live_concurrency: Any = None
) -> None:
    """Report the shared concurrency at the start, then finish once the parent has changed it."""
    event_queue.put(JobEvent(job_id=job_id, kind=EventKind.STATE, state=JobState.RUNNING))
    initial = live_concurrency.value
    event_queue.put(JobEvent(job_id=job_id, kind=EventKind.LOG, message=f"started with {initial}"))
    deadline = time.monotonic() + 60
    while live_concurrency.value == initial and time.monotonic() < deadline:
        time.sleep(0.02)
    event_queue.put(
        JobEvent(
            job_id=job_id,
            kind=EventKind.STATE,
            state=JobState.COMPLETED,
            message=f"ended with {live_concurrency.value}",
        )
    )


class _PipelineDownloader:
    """Stands in for RedditDownloader in a job's process: the real pipeline with nothing to download.

    The listing goes on until the parent has changed the limit and then a while
    longer, and the handlers report how many of them ran at once after that.
    """

    live_concurrency = None

    def __init__(self, config: Configuration, logging_handlers: list):
        pass

    def download(self) -> None:
        from bdfr.concurrency import process_submissions

        started_with = self.live_concurrency()
        lock = threading.Lock()
        running = peak = 0

        def listing():
            deadline = time.monotonic() + 60
            while self.live_concurrency() == started_with and time.monotonic() < deadline:
                yield "before"
            yield from ["after"] * 150

        def handler(_item: str) -> None:
            nonlocal running, peak
            with lock:
                running += 1
                if self.live_concurrency() != started_with:
                    peak = max(peak, running)
            time.sleep(0.02)
            with lock:
                running -= 1

        process_submissions([listing()], handler=handler, concurrency=self.live_concurrency)
        logging.getLogger(__name__).info(f"peak after the change: {peak}")


def fake_job_real_pipeline(job_id: str, config: Configuration, event_queue: Any, live_concurrency: Any = None) -> None:
    """Run the real job entry point, with a downloader that needs no Reddit."""
    with patch("bdfr.downloader.RedditDownloader", _PipelineDownloader):
        run_job(job_id, config, event_queue, live_concurrency)


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


def test_set_max_parallel_stays_within_the_range_of_the_setting():
    manager = JobManager(max_parallel=3, job_target=fake_job_success)
    for requested, expected in (
        (7, 7),
        (0, 1),
        (-5, 1),
        (MAX_PARALLEL_JOBS, MAX_PARALLEL_JOBS),
        (MAX_PARALLEL_JOBS + 1, MAX_PARALLEL_JOBS),
        (10**6, MAX_PARALLEL_JOBS),
    ):
        manager.set_max_parallel(requested)
        assert manager.max_parallel == expected


class _IdleProcess:
    """Stands in for a job's process, so that the scheduler can start jobs without anything running."""

    pid = 0
    exitcode = None

    def __init__(self, **_options):
        self._alive = False

    def start(self) -> None:
        self._alive = True

    def is_alive(self) -> bool:
        return self._alive

    def terminate(self) -> None:
        self._alive = False

    def join(self, timeout: float | None = None) -> None:
        pass


def _unlocked_value(_typecode: str, value: int, lock: bool = True) -> SimpleNamespace:
    """Stands in for the number a job shares with its process, and insists that it is made without a lock.

    A job that is terminated while it reads the number would leave a lock
    held, and the window would freeze at the next change of the number.
    """
    assert lock is False, "the shared concurrency has to be made with lock=False"
    return SimpleNamespace(value=value)


def test_number_above_the_range_starts_no_more_jobs_than_the_range_allows(tmp_path: Path):
    """A started job is not stopped again by lowering the number, so a mistyped one must not start them all."""
    manager = JobManager(max_parallel=2, job_target=fake_job_forever)
    manager._context = SimpleNamespace(Process=_IdleProcess, Value=_unlocked_value)
    for index in range(MAX_PARALLEL_JOBS + 10):
        manager.add_job(f"user{index}", _config_for(tmp_path))
    try:
        manager.pump()
        assert manager.running_count == 2
        manager.set_max_parallel(10**6)
        manager.pump()
        assert manager.running_count == MAX_PARALLEL_JOBS
    finally:
        manager.shutdown()


def test_job_without_a_configured_concurrency_starts_with_the_default(tmp_path: Path):
    """An unset concurrency means the default to the downloader, so it has to mean it to the shared number too."""
    manager = JobManager(max_parallel=2, job_target=fake_job_forever)
    manager._context = SimpleNamespace(Process=_IdleProcess, Value=_unlocked_value)
    unset, configured = (manager.add_job(label, _config_for(tmp_path)) for label in ("unset", "configured"))
    unset.config.concurrency = None
    configured.config.concurrency = 7
    try:
        manager.pump()
        assert (unset.state, configured.state) == (JobState.RUNNING, JobState.RUNNING)
        assert unset.live_concurrency.value == DEFAULT_CONCURRENCY
        assert configured.live_concurrency.value == 7
    finally:
        manager.shutdown()


@pytest.mark.slow
def test_raising_max_parallel_starts_more_queued_jobs(tmp_path: Path):
    manager = JobManager(max_parallel=1, job_target=fake_job_forever)
    jobs = [manager.add_job(f"user{i}", _config_for(tmp_path)) for i in range(4)]
    try:
        manager.pump()
        manager.pump()
        assert [job.state for job in jobs] == [JobState.RUNNING] + [JobState.PENDING] * 3
        manager.set_max_parallel(3)
        # Nothing starts until the schedule is advanced again.
        assert manager.running_count == 1
        manager.pump()
        assert [job.state for job in jobs] == [JobState.RUNNING] * 3 + [JobState.PENDING]
        assert all(job.process.is_alive() for job in jobs[:3])
    finally:
        manager.shutdown()


@pytest.mark.slow
def test_lowering_max_parallel_stops_nothing_and_delays_new_starts(tmp_path: Path):
    manager = JobManager(max_parallel=3, job_target=fake_job_forever)
    jobs = [manager.add_job(f"user{i}", _config_for(tmp_path)) for i in range(4)]
    try:
        manager.pump()
        assert manager.running_count == 3
        manager.set_max_parallel(1)
        manager.pump()
        # The jobs already running are left alone.
        assert manager.running_count == 3
        assert all(job.process.is_alive() for job in jobs[:3])
        # The queued job has to wait until fewer than the new limit are running.
        for still_running, job in ((2, jobs[0]), (1, jobs[1])):
            manager.cancel(job.job_id)
            manager.pump()
            assert manager.running_count == still_running
            assert jobs[3].state is JobState.PENDING
        manager.cancel(jobs[2].job_id)
        manager.pump()
        assert jobs[3].state is JobState.RUNNING
    finally:
        manager.shutdown()


def test_set_concurrency_updates_queued_jobs_within_bounds(tmp_path: Path):
    manager = JobManager(job_target=fake_job_success)
    jobs = [manager.add_job(f"user{i}", _config_for(tmp_path)) for i in range(3)]
    for requested, expected in ((9, 9), (0, 1), (-4, 1), (MAX_CONCURRENCY + 50, MAX_CONCURRENCY)):
        manager.set_concurrency(requested)
        assert [job.config.concurrency for job in jobs] == [expected] * 3
    # Nothing is shared with a process that does not exist yet.
    assert all(job.live_concurrency is None for job in jobs)


@pytest.mark.slow
def test_running_job_sees_a_changed_concurrency(tmp_path: Path):
    manager = JobManager(max_parallel=1, job_target=fake_job_report_concurrency)
    config = _config_for(tmp_path)
    config.concurrency = 3
    job = manager.add_job("alice", config)
    try:
        assert _pump_until(manager, lambda: job.last_message == "started with 3"), job.last_message
        # Shared without a lock: a job that is terminated while it reads the number would leave a
        # lock held, and the next change of the number would then wait for it for ever.
        assert not hasattr(job.live_concurrency, "get_lock")
        manager.set_concurrency(7)
        assert _pump_until(manager, lambda: job.state.is_terminal), job.last_message
        assert job.state is JobState.COMPLETED
        assert job.last_message == "ended with 7"
        # The shared value goes when the process does, and a later change is harmless.
        assert _pump_until(manager, lambda: job.process is None, timeout=60)
        assert job.live_concurrency is None
        manager.set_concurrency(5)
    finally:
        manager.shutdown()


@pytest.mark.slow
def test_queued_job_starts_with_the_changed_concurrency(tmp_path: Path):
    manager = JobManager(max_parallel=1, job_target=fake_job_report_concurrency)
    first, second = (manager.add_job(label, _config_for(tmp_path)) for label in ("first", "second"))
    first.config.concurrency = second.config.concurrency = 3
    try:
        assert _pump_until(manager, lambda: first.last_message == "started with 3"), first.last_message
        assert second.state is JobState.PENDING
        manager.set_concurrency(MAX_CONCURRENCY + 8)
        assert second.config.concurrency == MAX_CONCURRENCY
        assert _pump_until(manager, lambda: first.state.is_terminal), first.last_message
        assert first.last_message == f"ended with {MAX_CONCURRENCY}"
        started = f"started with {MAX_CONCURRENCY}"
        assert _pump_until(manager, lambda: second.last_message == started), second.last_message
        manager.set_concurrency(2)
        assert _pump_until(manager, lambda: second.state.is_terminal), second.last_message
        assert second.last_message == "ended with 2"
    finally:
        manager.shutdown()


@pytest.mark.slow
def test_changed_concurrency_reaches_the_pipeline_of_a_running_job(tmp_path: Path):
    """The whole way: the scheduler, the real job entry point in its own process, and the pipeline."""
    manager = JobManager(max_parallel=1, job_target=fake_job_real_pipeline)
    config = _config_for(tmp_path)
    config.concurrency = 2
    job = manager.add_job("alice", config)
    messages = []

    def pump_until(predicate, timeout: float = 90.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            messages.extend(event.message for event in manager.pump())
            if predicate():
                return True
            time.sleep(0.02)
        return False

    try:
        announced = "Starting with 2 concurrent workers, adjustable during the run"
        assert pump_until(lambda: announced in messages), messages
        manager.set_concurrency(5)
        assert pump_until(lambda: job.state.is_terminal), messages
        assert job.state is JobState.COMPLETED, messages
        assert "Concurrent workers changed from 2 to 5" in messages
        assert "peak after the change: 5" in messages
    finally:
        manager.shutdown()


@pytest.fixture()
def restored_logging():
    """run_job sets logging up for a process of its own; undo that when it is called in this one."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    quietened = {name: logging.getLogger(name).level for name in ("praw", "prawcore", "urllib3")}
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
    for name, previous in quietened.items():
        logging.getLogger(name).setLevel(previous)


class _FakeDownloader:
    """Stands in for RedditDownloader and records the live limit as download() finds it."""

    live_concurrency = None
    seen_by_download: list = []

    def __init__(self, config: Configuration, logging_handlers: list):
        pass

    def download(self) -> None:
        self.seen_by_download.append(self.live_concurrency)


@pytest.fixture()
def fake_downloader():
    _FakeDownloader.seen_by_download = []
    with patch("bdfr.downloader.RedditDownloader", _FakeDownloader):
        yield _FakeDownloader


def test_run_job_hands_the_shared_concurrency_to_the_downloader(
    tmp_path: Path, fake_downloader: type, restored_logging: None
):
    shared = SimpleNamespace(value=5)
    events = queue.Queue()

    run_job("job1", _config_for(tmp_path), events, shared)

    (live_concurrency,) = fake_downloader.seen_by_download
    assert live_concurrency() == 5
    # It is read anew on every call, which is how a running job follows a change.
    shared.value = 9
    assert live_concurrency() == 9
    states = [events.get_nowait().state for _ in range(events.qsize())]
    assert states == [JobState.RUNNING, JobState.COMPLETED]


def test_run_job_without_a_shared_concurrency_keeps_the_limit_fixed(
    tmp_path: Path, fake_downloader: type, restored_logging: None
):
    run_job("job1", _config_for(tmp_path), queue.Queue())

    assert fake_downloader.seen_by_download == [None]


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
