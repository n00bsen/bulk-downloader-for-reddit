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

from bdfr.constants import DEFAULT_CONCURRENCY, MAX_CONCURRENCY

logger = logging.getLogger(__name__)

T = TypeVar("T")

# How many queued items to allow per worker before the producer is throttled.
# A small buffer keeps workers fed without reading the whole listing into memory.
QUEUE_DEPTH_PER_WORKER = 4

# Seconds to pause after a listing-level PRAW failure, matching the historical
# sequential behaviour.
PRAW_ERROR_BACKOFF = 60

# Longest a run waits before it looks again at a limit that can change while
# it is under way. The GUI changes it from another process, which cannot wake
# a thread waiting in this one, so the run has to look again by itself.
LIVE_LIMIT_POLL_INTERVAL = 0.2


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


class _LiveSlots:
    """The places for handlers in a run whose concurrency can be changed while it is under way.

    It stands where the semaphore of a fixed limit stands: the producer takes a
    place before it hands an item to the pool, and the item's handler gives the
    place back when it ends. A thread pool cannot be resized, so the pool of
    such a run may grow as far as the limit can ever reach, and the places are
    what holds the handlers to the limit of the moment.

    An item that has to wait therefore waits on the producer and not on a pool
    thread. Handlers start in listing order, as they do under a fixed limit; no
    thread, with the HTTP session each one keeps, exists only to wait; and the
    listing is read no further ahead than that one item.

    Raising the limit lets more items through; lowering it interrupts nothing,
    it only holds new items back until enough running handlers have ended.
    Lowering it does not shrink the pool either. The threads started under the
    higher limit stay and take the items in turn, each with the HTTP session it
    has, so never more than the limit are at work at once, but the connections
    opened under the higher limit are kept until the run ends.
    """

    def __init__(self, read_limit: Callable[[], int]):
        self._read_limit = read_limit
        self._read_failed = False
        # Stands in for a limit that cannot be read, until it can be.
        self._limit = DEFAULT_CONCURRENCY
        self._limit = self._read()
        # Said here and not by the caller, whose configured number may already be out of date.
        logger.info(f"Starting with {self._limit} concurrent workers, adjustable during the run")
        self._taken = 0
        # Whether the item handed over last is still on its way to its handler.
        self._handing_over = False
        # Guards both; notified when a handler is reached and when a place is given back.
        self._moved_on = threading.Condition()

    def _read(self) -> int:
        """Return the limit as it is now, or the last one that could be read."""
        try:
            return max(1, min(MAX_CONCURRENCY, int(self._read_limit())))
        except Exception as e:  # a limit that cannot be read must never kill the run
            if not self._read_failed:
                self._read_failed = True
                logger.debug(f"Could not read the concurrency limit, keeping {self._limit}: {e}")
            return self._limit

    def _current_limit(self) -> int:
        """Look at the limit again. Only the producer does, so each change is logged once."""
        limit = self._read()
        if limit != self._limit:
            logger.info(f"Concurrent workers changed from {self._limit} to {limit}")
            self._limit = limit
        return limit

    def acquire(self) -> None:
        """Take a place for the next item, waiting for as long as the limit has none free. Called by the producer."""
        with self._moved_on:
            while self._handing_over or self._taken >= self._current_limit():
                # A handler that ends wakes this at once. The time limit is for
                # a limit raised in the meantime, which nothing announces.
                self._moved_on.wait(LIVE_LIMIT_POLL_INTERVAL)
            self._taken += 1
            self._handing_over = True

    def in_listing_order(self, handler: Callable[[T], None]) -> Callable[[T], None]:
        """Return `handler` so wrapped that no item is handed over before the one ahead of it has reached it.

        Two items handed to two idle threads in the same instant would
        otherwise start in whichever order those threads happen to wake up.
        """

        def handle(item: T) -> None:
            with self._moved_on:
                self._handing_over = False
                self._moved_on.notify()
            handler(item)

        return handle

    def release(self) -> None:
        """Give a place back. Called on a handler's thread once the handler has ended."""
        with self._moved_on:
            self._taken -= 1
            self._moved_on.notify()


def process_submissions(
    generators: Iterable[Iterator[T]],
    handler: Callable[[T], None],
    concurrency: int | Callable[[], int],
    prepare: Callable[[T], bool] | None = None,
    describe: Callable[[T], str] = lambda item: str(item),
) -> PipelineStats:
    """Run `handler` over every item from `generators`, `concurrency` at a time.

    `prepare` runs on the producer thread and returns False to drop an item
    before it reaches a worker; it is where filtering belongs, so that rejected
    items never occupy a worker slot. `handler` runs on a worker thread.

    A semaphore bounds the number of in-flight items so that a long listing is
    not drained into memory faster than it can be processed.

    `concurrency` is a number, or a callable returning the limit as it is right
    now for a caller whose limit can change during the run. A changed limit
    takes effect within `LIVE_LIMIT_POLL_INTERVAL` seconds and is kept between
    1 and `MAX_CONCURRENCY`; `_LiveSlots` then takes the semaphore's place.
    """
    stats = PipelineStats()
    if callable(concurrency):
        in_flight = _LiveSlots(concurrency)
        handler = in_flight.in_listing_order(handler)
        # The most the limit can become. The pool starts a thread only when it
        # is handed an item while none of its threads is idle, so it stays as
        # small as the number of handlers that have run at once. Now and then
        # it is a thread larger: the pool takes a thread for idle an instant
        # after its handler has given the place back.
        pool_size = MAX_CONCURRENCY
    else:
        if concurrency < 1:
            raise ValueError(f"Concurrency must be at least 1, got {concurrency}")
        in_flight = threading.Semaphore(concurrency * QUEUE_DEPTH_PER_WORKER)
        pool_size = concurrency

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

    with ThreadPoolExecutor(max_workers=pool_size, thread_name_prefix="bdfr-worker") as pool:
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
