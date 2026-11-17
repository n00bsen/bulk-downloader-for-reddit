#!/usr/bin/env python3

"""Run several BDFR downloads at the same time, each in its own process.

Process isolation rather than threads, for three reasons: a wedged yt-dlp
extraction cannot stall the other downloads, cancelling a job is a clean
terminate, and each process gets its own PRAW instance and log file instead of
sharing mutable state.

Progress is reported back over a `multiprocessing.Queue` as `JobEvent`s. The
child forwards its log records, which already carry structured markers for
completed submissions, so no log-message parsing is needed.

The other direction is one shared integer per job: the number of resources the
job may fetch at once, which the parent can change while the job is running.
"""

import logging
import multiprocessing
import queue
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from bdfr.configuration import Configuration
from bdfr.constants import DEFAULT_CONCURRENCY, DEFAULT_MAX_PARALLEL_JOBS, MAX_CONCURRENCY, MAX_PARALLEL_JOBS

logger = logging.getLogger(__name__)


def _clamp_concurrency(value: int) -> int:
    """Bring a requested concurrency into the range a running job can follow."""
    return max(1, min(MAX_CONCURRENCY, int(value)))


class JobState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in (JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED)


class EventKind(StrEnum):
    STATE = "state"
    LOG = "log"
    PROGRESS = "progress"


@dataclass(frozen=True)
class JobEvent:
    """A message from a worker process about one job."""

    job_id: str
    kind: EventKind
    message: str = ""
    state: JobState | None = None
    completed: int = 0
    level: int = logging.INFO


@dataclass
class Job:
    """A single download, and whatever is currently known about it."""

    label: str
    config: Configuration
    job_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    state: JobState = JobState.PENDING
    completed: int = 0
    last_message: str = ""
    process: Any = field(default=None, repr=False)
    # The job's concurrency, shared with its process so that it can be changed
    # while the job runs. None before the job starts and after it has ended.
    live_concurrency: Any = field(default=None, repr=False)

    @property
    def is_active(self) -> bool:
        return self.state is JobState.RUNNING


class _QueueLogHandler(logging.Handler):
    """Forward a child process log record to the parent as a JobEvent."""

    def __init__(self, job_id: str, event_queue: Any):
        super().__init__(level=logging.INFO)
        self.job_id = job_id
        self.event_queue = event_queue
        self.completed = 0
        # Every one is kept: a listing that gave up is logged before the
        # downloader's skip summary, and must not be replaced by it.
        self.summaries: list[str] = []

    @property
    def completion_message(self) -> str:
        """The job's final message, which the window keeps showing once it has finished.

        A plain "Finished" would replace a run summary such as why nothing was
        downloaded, so the summaries are carried into it.
        """
        return " ".join(("Finished.", *self.summaries)) if self.summaries else "Finished"

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if getattr(record, "bdfr_event", None) == "run_summary":
                self.summaries.append(record.getMessage())
            if getattr(record, "bdfr_event", None) == "submission_complete":
                self.completed += 1
                self.event_queue.put(
                    JobEvent(
                        job_id=self.job_id,
                        kind=EventKind.PROGRESS,
                        message=record.getMessage(),
                        completed=self.completed,
                        level=record.levelno,
                    )
                )
                return
            self.event_queue.put(
                JobEvent(
                    job_id=self.job_id,
                    kind=EventKind.LOG,
                    message=record.getMessage(),
                    level=record.levelno,
                )
            )
        except Exception:
            # A failure to report progress must never break the download.
            self.handleError(record)


def run_job(job_id: str, config: Configuration, event_queue: Any, live_concurrency: Any = None) -> None:
    """Entry point executed in the child process.

    The downloader is imported here rather than at module scope so that the
    parent process (notably the GUI) does not pay for PRAW and yt-dlp imports
    it never uses.

    `live_concurrency` is the shared integer the parent changes to adjust the
    job's concurrency while it runs; without one the job keeps the concurrency
    in `config` for its whole run.
    """
    from bdfr.downloader import RedditDownloader

    handler = _QueueLogHandler(job_id, event_queue)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    for noisy in ("praw", "prawcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.CRITICAL)

    event_queue.put(JobEvent(job_id=job_id, kind=EventKind.STATE, state=JobState.RUNNING))
    try:
        downloader = RedditDownloader(config, [])
        if live_concurrency is not None:
            downloader.live_concurrency = lambda: live_concurrency.value
        downloader.download()
    except BaseException as e:
        event_queue.put(
            JobEvent(
                job_id=job_id,
                kind=EventKind.STATE,
                state=JobState.FAILED,
                message=f"{type(e).__name__}: {e}",
                completed=handler.completed,
                level=logging.ERROR,
            )
        )
        raise
    event_queue.put(
        JobEvent(
            job_id=job_id,
            kind=EventKind.STATE,
            state=JobState.COMPLETED,
            message=handler.completion_message,
            completed=handler.completed,
        )
    )


class JobManager:
    """Schedule jobs across a bounded pool of processes."""

    def __init__(
        self,
        max_parallel: int = DEFAULT_MAX_PARALLEL_JOBS,
        job_target: Callable[[str, Configuration, Any, Any], None] = run_job,
    ):
        self.max_parallel = max(1, int(max_parallel))
        # The target is injectable so the scheduler can be exercised without
        # contacting Reddit. It must be a module-level function to survive spawn.
        self.job_target = job_target
        self.jobs: dict[str, Job] = {}
        self._order: list[str] = []
        # "spawn" is the only start method available on Windows, and asking for
        # it explicitly keeps behaviour identical on other platforms.
        self._context = multiprocessing.get_context("spawn")
        self._queue = self._context.Queue()

    def add_job(self, label: str, config: Configuration) -> Job:
        job = Job(label=label, config=config)
        self.jobs[job.job_id] = job
        self._order.append(job.job_id)
        return job

    def set_max_parallel(self, max_parallel: int) -> None:
        """Change how many jobs may run at once; takes effect on the next pump.

        Raising it starts more queued jobs. Lowering it stops nothing: running
        jobs carry on, and queued ones wait until fewer than the new limit run.
        A raise can therefore not be taken back, which is why the number is
        kept between 1 and `MAX_PARALLEL_JOBS` whatever is asked for.
        """
        self.max_parallel = max(1, min(MAX_PARALLEL_JOBS, int(max_parallel)))

    def set_concurrency(self, concurrency: int) -> None:
        """Change how many resources each job fetches at once, running jobs included.

        A running job follows by itself: it looks at the number again before
        every submission it starts, and several times a second while one is
        waiting for its turn. Lowering it interrupts no transfer that is
        already under way. The number is kept between 1 and `MAX_CONCURRENCY`.
        """
        value = _clamp_concurrency(concurrency)
        for job in self.jobs.values():
            if job.state is JobState.PENDING:
                # Read when the job is launched, to initialise its shared value.
                job.config.concurrency = value
            elif job.live_concurrency is not None:
                job.live_concurrency.value = value

    @property
    def ordered_jobs(self) -> list[Job]:
        return [self.jobs[job_id] for job_id in self._order]

    @property
    def running_count(self) -> int:
        return sum(1 for job in self.jobs.values() if job.state is JobState.RUNNING)

    @property
    def is_finished(self) -> bool:
        return all(job.state.is_terminal for job in self.jobs.values())

    def pump(self) -> list[JobEvent]:
        """Advance the schedule and return events received since the last call.

        Called repeatedly by the UI, so it never blocks.
        """
        events = list(self._drain_queue())
        self._reap_finished()
        self._launch_pending()
        return events

    def _drain_queue(self) -> Iterator[JobEvent]:
        while True:
            try:
                event = self._queue.get_nowait()
            except queue.Empty:
                return
            job = self.jobs.get(event.job_id)
            if job is None:
                continue
            if event.kind is EventKind.PROGRESS:
                job.completed = event.completed
                job.last_message = event.message
            elif event.kind is EventKind.LOG:
                job.last_message = event.message
            elif event.kind is EventKind.STATE and event.state is not None:
                # A cancel has already decided this job outcome; keep it.
                if job.state is not JobState.CANCELLED:
                    job.state = event.state
                    if event.message:
                        job.last_message = event.message
            yield event

    def _reap_finished(self) -> None:
        for job in self.jobs.values():
            process = job.process
            if process is None or process.is_alive():
                continue
            if job.state is JobState.RUNNING:
                # The process ended without reporting a terminal state.
                if process.exitcode == 0:
                    job.state = JobState.COMPLETED
                    job.last_message = job.last_message or "Finished"
                else:
                    job.state = JobState.FAILED
                    job.last_message = f"Exited with code {process.exitcode}"
            process.join(timeout=0)
            job.process = None
            job.live_concurrency = None

    def _launch_pending(self) -> None:
        for job in self.ordered_jobs:
            if self.running_count >= self.max_parallel:
                return
            if job.state is not JobState.PENDING:
                continue
            job.state = JobState.RUNNING
            job.last_message = "Starting"
            # Shared without a lock. One integer is read and written whole, and
            # a lock could be left held by a job that is terminated while
            # reading, which would freeze the window at the next change.
            # An unset concurrency means the default, as it does to the downloader.
            job.live_concurrency = self._context.Value(
                "i", _clamp_concurrency(job.config.concurrency or DEFAULT_CONCURRENCY), lock=False
            )
            # Deliberately not daemonic: a download started with
            # --search-existing creates its own process pool for hashing, and
            # daemonic processes are not allowed to have children.
            job.process = self._context.Process(
                target=self.job_target,
                args=(job.job_id, job.config, self._queue, job.live_concurrency),
                name=f"bdfr-job-{job.label}",
                daemon=False,
            )
            job.process.start()
            logger.debug(f"Started job {job.job_id} for {job.label} as pid {job.process.pid}")

    def cancel(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if job is None:
            return False
        if job.state is JobState.PENDING:
            job.state = JobState.CANCELLED
            job.last_message = "Cancelled before starting"
            return True
        if job.state is not JobState.RUNNING:
            return False
        job.state = JobState.CANCELLED
        job.last_message = "Cancelled"
        process = job.process
        if process is not None and process.is_alive():
            process.terminate()
        return True

    def cancel_all(self) -> None:
        for job_id in list(self.jobs):
            self.cancel(job_id)

    def shutdown(self, timeout: float = 5.0) -> None:
        """Stop everything and release the queue. Safe to call more than once."""
        self.cancel_all()
        for job in self.jobs.values():
            process = job.process
            if process is None:
                continue
            process.join(timeout=timeout)
            if process.is_alive():
                process.kill()
                process.join(timeout=timeout)
            job.process = None
            job.live_concurrency = None
        try:
            self._queue.close()
            self._queue.join_thread()
        except Exception:
            logger.debug("Event queue was already closed")
