#!/usr/bin/env python3

import threading
from unittest.mock import MagicMock

import prawcore
import pytest

from bdfr.concurrency import (
    QUEUE_DEPTH_PER_WORKER,
    make_reddit_instance_thread_safe,
    process_submissions,
)


def test_process_submissions_handles_every_item():
    handled = []
    lock = threading.Lock()

    def handler(item):
        with lock:
            handled.append(item)

    stats = process_submissions([iter(range(50))], handler=handler, concurrency=4)
    assert sorted(handled) == list(range(50))
    assert stats.completed == 50
    assert stats.failed == 0
    assert stats.skipped == 0


def test_process_submissions_spans_multiple_generators():
    handled = []
    lock = threading.Lock()

    def handler(item):
        with lock:
            handled.append(item)

    stats = process_submissions([iter([1, 2]), iter([3, 4]), iter([5])], handler=handler, concurrency=3)
    assert sorted(handled) == [1, 2, 3, 4, 5]
    assert stats.completed == 5


def test_prepare_filters_items_before_workers_see_them():
    handled = []
    lock = threading.Lock()

    def handler(item):
        with lock:
            handled.append(item)

    stats = process_submissions(
        [iter(range(10))],
        handler=handler,
        concurrency=4,
        prepare=lambda item: item % 2 == 0,
    )
    assert sorted(handled) == [0, 2, 4, 6, 8]
    assert stats.skipped == 5
    assert stats.produced == 5


def test_one_failing_item_does_not_stop_the_run():
    handled = []
    lock = threading.Lock()

    def handler(item):
        if item == 5:
            raise ValueError("boom")
        with lock:
            handled.append(item)

    stats = process_submissions([iter(range(10))], handler=handler, concurrency=4)
    assert 5 not in handled
    assert len(handled) == 9
    assert stats.failed == 1
    assert stats.completed == 9


def test_praw_exception_in_handler_is_counted_not_raised():
    def handler(item):
        raise prawcore.exceptions.RequestException(Exception("net"), (), {})

    stats = process_submissions([iter([1, 2])], handler=handler, concurrency=2)
    assert stats.failed == 2
    assert stats.completed == 0


def test_work_actually_runs_in_parallel():
    """With N workers, N blocking items must be in flight simultaneously."""
    concurrency = 4
    barrier = threading.Barrier(concurrency, timeout=30)

    def handler(_item):
        # Only passes if `concurrency` workers reach the barrier at once.
        barrier.wait()

    stats = process_submissions([iter(range(concurrency))], handler=handler, concurrency=concurrency)
    assert stats.completed == concurrency
    assert stats.failed == 0


def test_producer_is_bounded_and_does_not_drain_generator():
    """The generator must not be read arbitrarily far ahead of the workers."""
    concurrency = 2
    max_in_flight = concurrency * QUEUE_DEPTH_PER_WORKER
    produced = []
    release = threading.Event()

    def generator():
        for i in range(100):
            produced.append(i)
            yield i

    def handler(_item):
        release.wait(timeout=30)

    def run():
        return process_submissions([generator()], handler=handler, concurrency=concurrency)

    thread = threading.Thread(target=run)
    thread.start()
    # Give the producer a chance to run ahead, then confirm it was throttled.
    threading.Event().wait(0.5)
    assert len(produced) <= max_in_flight + concurrency + 1, f"producer read {len(produced)} items ahead"
    release.set()
    thread.join(timeout=60)
    assert not thread.is_alive()


def test_concurrency_below_one_is_rejected():
    with pytest.raises(ValueError, match="at least 1"):
        process_submissions([iter([1])], handler=lambda item: None, concurrency=0)


def test_generator_level_praw_error_is_survived(monkeypatch):
    """A listing that blows up mid-iteration must not abort the whole run."""
    monkeypatch.setattr("bdfr.concurrency.PRAW_ERROR_BACKOFF", 0)
    handled = []

    def exploding():
        yield 1
        raise prawcore.exceptions.RequestException(Exception("listing died"), (), {})

    stats = process_submissions(
        [exploding(), iter([2, 3])],
        handler=handled.append,
        concurrency=2,
    )
    assert sorted(handled) == [1, 2, 3]
    assert stats.completed == 3


def test_make_reddit_instance_thread_safe_serialises_requests():
    """Concurrent Reddit calls must never overlap."""
    overlaps = []
    active = 0
    guard = threading.Lock()

    def slow_request(*_args, **_kwargs):
        nonlocal active
        with guard:
            active += 1
            if active > 1:
                overlaps.append(active)
        threading.Event().wait(0.01)
        with guard:
            active -= 1
        return "ok"

    reddit = MagicMock()
    reddit._core.request = slow_request
    reddit._core._bdfr_serialised = False
    make_reddit_instance_thread_safe(reddit)

    threads = [threading.Thread(target=lambda: reddit._core.request("GET", "/x")) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert overlaps == [], f"Reddit requests overlapped: {overlaps}"


def test_make_reddit_instance_thread_safe_is_idempotent():
    calls = []
    reddit = MagicMock()
    reddit._core.request = lambda *a, **k: calls.append(a)
    reddit._core._bdfr_serialised = False

    make_reddit_instance_thread_safe(reddit)
    first_wrapper = reddit._core.request
    make_reddit_instance_thread_safe(reddit)

    assert reddit._core.request is first_wrapper
    reddit._core.request("GET", "/x")
    assert len(calls) == 1


def test_make_reddit_instance_thread_safe_tolerates_missing_session(caplog):
    """A PRAW internal change must degrade with a warning, not crash."""

    class NoCore:
        _core = None

    result = make_reddit_instance_thread_safe(NoCore())
    assert isinstance(result, NoCore)
