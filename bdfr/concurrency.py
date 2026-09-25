#!/usr/bin/env python3

"""Concurrency primitives for the download path.

Downloads are almost entirely I/O bound -- waiting on media hosts -- so threads
are the right tool. The complication is PRAW: a `Reddit` instance is not
thread-safe, and submissions produced by a listing are not fully fetched, so
touching an attribute that was absent from the listing JSON triggers a lazy API
call. That can happen deep inside a site downloader, on a worker thread.

Rather than eagerly fetching every submission (which would double the number of
Reddit API calls), all Reddit HTTP is funnelled through a single lock at the
prawcore session boundary. Media downloads -- the actual bulk of the work -- do
not touch that path at all, so contention is negligible.
"""

import functools
import logging
import threading
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from time import sleep
from typing import Any, TypeVar

import prawcore

logger = logging.getLogger(__name__)

T = TypeVar("T")

# How many queued items to allow per worker before the producer is throttled.
# A small buffer keeps workers fed without reading the whole listing into memory.
QUEUE_DEPTH_PER_WORKER = 4

# Seconds to pause after a listing-level PRAW failure, matching the historical
# sequential behaviour.
PRAW_ERROR_BACKOFF = 60


@dataclass
class PipelineStats:
    """Counters describing one pipeline run."""

    produced: int = 0
    skipped: int = 0
    completed: int = 0
    failed: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record_completed(self) -> None:
        with self._lock:
            self.completed += 1

    def record_failed(self) -> None:
        with self._lock:
            self.failed += 1


def make_reddit_instance_thread_safe(reddit: Any) -> Any:
    """Serialise every HTTP request made through a PRAW `Reddit` instance.

    Wrapping the prawcore session's `request` method is deliberately the
    narrowest possible choke point: it covers every lazy attribute fetch no
    matter which attribute triggered it, without changing any call site.
    """
    core = getattr(reddit, "_core", None)
    if core is None or not hasattr(core, "request"):
        logger.warning(
            "Could not find the PRAW session to serialise; concurrent runs will "
            "rely on Reddit access staying on the producer thread"
        )
        return reddit
    if getattr(core, "_bdfr_serialised", False):
        return reddit

    lock = threading.RLock()
    original_request = core.request

    @functools.wraps(original_request)
    def locked_request(*args, **kwargs):
        with lock:
            return original_request(*args, **kwargs)

    core.request = locked_request
    core._bdfr_serialised = True
    logger.log(9, "Reddit session calls are now serialised for concurrent use")
    return reddit


def process_submissions(
    generators: Iterable[Iterator[T]],
    handler: Callable[[T], None],
    concurrency: int,
    prepare: Callable[[T], bool] | None = None,
    describe: Callable[[T], str] = lambda item: str(item),
) -> PipelineStats:
    """Run `handler` over every item from `generators`, `concurrency` at a time.

    `prepare` runs on the producer thread and returns False to drop an item
    before it reaches a worker; it is where filtering belongs, so that rejected
    items never occupy a worker slot. `handler` runs on a worker thread.

    A semaphore bounds the number of in-flight items so that a long listing is
    not drained into memory faster than it can be processed.
    """
    stats = PipelineStats()
    if concurrency < 1:
        raise ValueError(f"Concurrency must be at least 1, got {concurrency}")

    in_flight = threading.Semaphore(concurrency * QUEUE_DEPTH_PER_WORKER)

    def run_one(item: T) -> None:
        try:
            handler(item)
            stats.record_completed()
        except prawcore.PrawcoreException as e:
            stats.record_failed()
            logger.error(f"Submission {describe(item)} failed to download due to a PRAW exception: {e}")
        except Exception as e:  # a single bad submission must never kill the run
            stats.record_failed()
            logger.error(f"Submission {describe(item)} failed to download: {e}")
            logger.exception("Submission failure")
        finally:
            in_flight.release()

    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="bdfr-worker") as pool:
        for generator in generators:
            last_description = "unknown"
            try:
                for item in generator:
                    last_description = describe(item)
                    if prepare is not None and not prepare(item):
                        stats.skipped += 1
                        continue
                    stats.produced += 1
                    in_flight.acquire()
                    pool.submit(run_one, item)
            except prawcore.PrawcoreException as e:
                logger.error(f"The submission after {last_description} failed to download due to a PRAW exception: {e}")
                logger.debug(f"Waiting {PRAW_ERROR_BACKOFF} seconds to continue")
                sleep(PRAW_ERROR_BACKOFF)
    return stats
