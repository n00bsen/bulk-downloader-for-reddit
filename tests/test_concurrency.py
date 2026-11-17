#!/usr/bin/env python3

import itertools
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

import prawcore
import pytest

from bdfr.concurrency import (
    LIVE_LIMIT_POLL_INTERVAL,
    QUEUE_DEPTH_PER_WORKER,
    PipelineStats,
    make_reddit_instance_thread_safe,
    process_submissions,
)
from bdfr.constants import DEFAULT_CONCURRENCY, MAX_CONCURRENCY


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


class _HeldHandlers:
    """A handler that counts how many calls are running and ends one only when told to."""

    def __init__(self):
        self._lock = threading.Lock()
        self._may_finish = threading.Semaphore(0)
        self.running = 0
        self.peak = 0
        self.started = []
        self.handled = []

    def __call__(self, item):
        with self._lock:
            self.running += 1
            self.peak = max(self.peak, self.running)
            self.started.append(item)
        self._may_finish.acquire()
        with self._lock:
            self.running -= 1
            self.handled.append(item)

    def let_finish(self, count: int) -> None:
        self._may_finish.release(count)

    def wait_for_running(self, count: int, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.running == count:
                return True
            time.sleep(0.005)
        return False


def _start_pipeline(results: list, **kwargs) -> threading.Thread:
    # A daemon, so that a pipeline that never ends fails its test instead of hanging the session.
    thread = threading.Thread(target=lambda: results.append(process_submissions(**kwargs)), daemon=True)
    thread.start()
    return thread


def _run_pipeline(**kwargs) -> PipelineStats:
    """Run a pipeline to its end, which a pipeline that has none fails to reach within the time allowed."""
    results = []
    thread = _start_pipeline(results, **kwargs)
    thread.join(timeout=60)
    assert not thread.is_alive(), "the pipeline did not end"
    return results[0]


def _give_the_gate_time_to_misbehave() -> None:
    """Wait long enough for the limit to have been looked at several times."""
    time.sleep(4 * LIVE_LIMIT_POLL_INTERVAL)


def test_live_limit_is_looked_at_often_enough():
    """Nothing tells a job that the limit changed; it finds out by looking again."""
    assert 0 < LIVE_LIMIT_POLL_INTERVAL <= 0.25


@pytest.mark.parametrize(
    ("limit", "expected_peak"),
    ((1, 1), (3, 3), (0, 1), (-2, 1), (MAX_CONCURRENCY, MAX_CONCURRENCY), (MAX_CONCURRENCY + 100, MAX_CONCURRENCY)),
)
def test_live_limit_bounds_the_handlers_running_at_once(limit: int, expected_peak: int):
    handlers = _HeldHandlers()
    results = []
    count = 3 * MAX_CONCURRENCY
    thread = _start_pipeline(results, generators=[iter(range(count))], handler=handlers, concurrency=lambda: limit)
    try:
        assert handlers.wait_for_running(expected_peak), f"{handlers.running} handlers running"
        _give_the_gate_time_to_misbehave()
        assert handlers.running == expected_peak
    finally:
        handlers.let_finish(count)
        thread.join(timeout=60)
    assert not thread.is_alive()
    assert handlers.peak == expected_peak
    # Every item was handled, and none of them twice.
    assert sorted(handlers.handled) == list(range(count))
    assert results[0].completed == count
    assert results[0].failed == 0


def test_raising_a_live_limit_lets_more_handlers_start():
    limit = [2]
    handlers = _HeldHandlers()
    results = []
    count = 20
    thread = _start_pipeline(results, generators=[iter(range(count))], handler=handlers, concurrency=lambda: limit[0])
    try:
        assert handlers.wait_for_running(2)
        _give_the_gate_time_to_misbehave()
        assert handlers.running == 2
        limit[0] = 6
        raised = time.monotonic()
        # No handler finishes here, so only looking at the limit again can start more.
        assert handlers.wait_for_running(6), f"{handlers.running} handlers running"
        assert time.monotonic() - raised < 1.0
        _give_the_gate_time_to_misbehave()
        assert handlers.running == 6
    finally:
        handlers.let_finish(count)
        thread.join(timeout=60)
    assert not thread.is_alive()
    assert handlers.peak == 6
    assert sorted(handlers.handled) == list(range(count))


def test_lowering_a_live_limit_only_holds_back_new_handlers():
    limit = [4]
    handlers = _HeldHandlers()
    results = []
    thread = _start_pipeline(results, generators=[iter(range(40))], handler=handlers, concurrency=lambda: limit[0])
    try:
        assert handlers.wait_for_running(4)
        limit[0] = 2
        _give_the_gate_time_to_misbehave()
        # The four that were running were not interrupted.
        assert handlers.running == 4
        assert handlers.handled == []
        # One ending leaves three, still more than the new limit: nothing may start.
        handlers.let_finish(1)
        assert handlers.wait_for_running(3)
        _give_the_gate_time_to_misbehave()
        assert handlers.running == 3
        # Two more ending leave one, so exactly one waiting handler starts.
        handlers.let_finish(2)
        assert handlers.wait_for_running(2)
        _give_the_gate_time_to_misbehave()
        assert handlers.running == 2
        assert len(handlers.handled) == 3
    finally:
        handlers.let_finish(40)
        thread.join(timeout=60)
    assert not thread.is_alive()
    assert handlers.peak == 4
    assert sorted(handlers.handled) == list(range(40))
    assert results[0].completed == 40


def test_producer_reads_one_item_ahead_at_most_under_a_live_limit():
    """An item that has to wait for a place waits with the producer, which reads no further until it has gone."""
    limit = [2]
    produced = []
    handlers = _HeldHandlers()
    results = []

    def generator():
        for i in range(100):
            produced.append(i)
            yield i

    thread = _start_pipeline(results, generators=[generator()], handler=handlers, concurrency=lambda: limit[0])
    try:
        assert handlers.wait_for_running(2)
        _give_the_gate_time_to_misbehave()
        # The two that are being handled, and the one that is waiting for a place.
        assert produced == [0, 1, 2]
        limit[0] = 5
        assert handlers.wait_for_running(5)
        _give_the_gate_time_to_misbehave()
        assert produced == [0, 1, 2, 3, 4, 5]
    finally:
        handlers.let_finish(100)
        thread.join(timeout=60)
    assert not thread.is_alive()
    assert len(produced) == 100


def test_live_limit_that_fails_to_read_keeps_the_last_value(caplog: pytest.LogCaptureFixture):
    calls = itertools.count()

    def limit() -> int:
        if next(calls) % 2:
            raise RuntimeError("limit unavailable")
        return 3

    caplog.set_level(logging.DEBUG, logger="bdfr.concurrency")
    handlers = _HeldHandlers()
    results = []
    thread = _start_pipeline(results, generators=[iter(range(30))], handler=handlers, concurrency=limit)
    try:
        assert handlers.wait_for_running(3)
        _give_the_gate_time_to_misbehave()
        assert handlers.running == 3
    finally:
        handlers.let_finish(30)
        thread.join(timeout=60)
    assert not thread.is_alive()
    assert results[0].completed == 30
    assert handlers.peak == 3
    # Said once, not on every look at the limit.
    assert caplog.text.count("Could not read the concurrency limit") == 1


def _unavailable() -> int:
    raise RuntimeError("limit unavailable")


@pytest.mark.parametrize(
    "limit",
    (_unavailable, lambda: None, lambda: "many", lambda: float("inf"), lambda: float("nan")),
    ids=("raises", "nothing", "text", "infinity", "not a number"),
)
def test_live_limit_that_never_reads_falls_back_to_the_default(limit):
    handlers = _HeldHandlers()
    results = []
    thread = _start_pipeline(results, generators=[iter(range(30))], handler=handlers, concurrency=limit)
    try:
        assert handlers.wait_for_running(DEFAULT_CONCURRENCY)
    finally:
        handlers.let_finish(30)
        thread.join(timeout=60)
    assert not thread.is_alive()
    assert results[0].completed == 30
    assert handlers.peak == DEFAULT_CONCURRENCY


def test_failing_handler_gives_its_place_back_under_a_live_limit():
    def handler(item):
        if item % 3 == 0:
            raise ValueError("boom")

    stats = _run_pipeline(generators=[iter(range(60))], handler=handler, concurrency=lambda: 2)
    assert stats.failed == 20
    assert stats.completed == 40


def test_handler_that_ends_its_thread_gives_its_place_back_under_a_live_limit():
    """Not even SystemExit, which the pipeline does not count as a failure, may leave a place taken for good."""
    handled = []

    def handler(item):
        if item % 2:
            raise SystemExit(1)
        handled.append(item)

    # With a single place, losing it once would leave every later item waiting.
    stats = _run_pipeline(generators=[iter(range(8))], handler=handler, concurrency=lambda: 1)
    assert handled == [0, 2, 4, 6]
    assert (stats.produced, stats.completed, stats.failed) == (8, 4, 0)


def test_live_limit_run_ends_without_waiting_for_the_next_look():
    """A handler that ends makes room for the next item at once; the periodic look is only for a changed limit."""
    handled = []

    def handler(item):
        # Long enough for the next item to be waiting for a place by the time this one ends:
        # a handler that ended before anything waited would have nobody to wake.
        time.sleep(0.002)
        handled.append(item)

    started = time.monotonic()
    stats = _run_pipeline(generators=[iter(range(400))], handler=handler, concurrency=lambda: 2)
    elapsed = time.monotonic() - started
    assert stats.completed == 400
    assert sorted(handled) == list(range(400))
    # Waiting out one look for every two items, which is what the two places would come to.
    waited_out = 400 * LIVE_LIMIT_POLL_INTERVAL / 2
    assert elapsed < waited_out / 4, f"run took {elapsed:.1f} seconds"


def test_live_limit_of_one_handles_items_in_listing_order():
    """As the sequential path does, which a job with one connection took before its limit could be changed."""
    handled = []

    def handler(item):
        # Long enough for the next item to be waiting for the one place by the time this one ends.
        time.sleep(0.001)
        handled.append(item)

    stats = _run_pipeline(generators=[iter(range(150)), iter(range(150, 200))], handler=handler, concurrency=lambda: 1)
    assert handled == list(range(200))
    assert stats.completed == 200


def test_items_start_in_listing_order_after_a_live_limit_was_lowered():
    limit = [4]
    handlers = _HeldHandlers()
    results = []
    thread = _start_pipeline(results, generators=[iter(range(40))], handler=handlers, concurrency=lambda: limit[0])
    try:
        assert handlers.wait_for_running(4)
        limit[0] = 1
    finally:
        handlers.let_finish(40)
        thread.join(timeout=60)
    assert not thread.is_alive()
    assert sorted(handlers.started[:4]) == [0, 1, 2, 3]
    # One at a time from here on, so the order in which they started is the order in which they were taken.
    assert handlers.started[4:] == list(range(4, 40))
    assert handlers.peak == 4


def test_item_is_handed_over_only_once_the_one_ahead_has_reached_its_handler(monkeypatch: pytest.MonkeyPatch):
    """Two items handed to two idle threads at once would start in whichever order those threads wake up."""
    handed_over = []
    may_reach_handler = threading.Semaphore(0)

    class SlowToStart(ThreadPoolExecutor):
        """A pool whose threads take their time to get round to an item."""

        def submit(self, fn, item):
            handed_over.append(item)

            def task() -> None:
                may_reach_handler.acquire()
                fn(item)

            return super().submit(task)

    monkeypatch.setattr("bdfr.concurrency.ThreadPoolExecutor", SlowToStart)
    handlers = _HeldHandlers()
    results = []
    thread = _start_pipeline(results, generators=[iter(range(10))], handler=handlers, concurrency=lambda: 5)
    try:
        _give_the_gate_time_to_misbehave()
        # There are places for five, but the first item is still on its way to its handler.
        assert handed_over == [0]
        may_reach_handler.release()
        assert handlers.wait_for_running(1)
        _give_the_gate_time_to_misbehave()
        assert handed_over == [0, 1]
    finally:
        may_reach_handler.release(10)
        handlers.let_finish(10)
        thread.join(timeout=60)
    assert not thread.is_alive()
    assert handed_over == list(range(10))
    assert sorted(handlers.handled) == list(range(10))


def test_no_thread_waits_for_a_place_under_a_live_limit():
    """Each pool thread keeps an HTTP session of its own, so one that only waits is a spare set of open connections."""
    others = set(threading.enumerate())
    handlers = _HeldHandlers()
    results = []
    thread = _start_pipeline(results, generators=[iter(range(40))], handler=handlers, concurrency=lambda: 3)
    try:
        assert handlers.wait_for_running(3)
        _give_the_gate_time_to_misbehave()
        # Besides the producer, the run has the three threads whose handlers are running and no other.
        started_by_the_run = [t.name for t in threading.enumerate() if t not in others and t is not thread]
        assert len(started_by_the_run) == 3, started_by_the_run
    finally:
        handlers.let_finish(40)
        thread.join(timeout=60)
    assert not thread.is_alive()
    assert results[0].completed == 40


def test_pool_only_grows_when_no_idle_thread_can_take_an_item_under_a_live_limit():
    """Its size is the highest the limit can reach, which is not how many threads a run should end up with."""
    threads = set()
    handled = threading.Event()

    def handler(_item):
        threads.add(threading.current_thread().name)
        handled.set()

    def listing():
        for item in range(30):
            handled.clear()
            yield item
            # Read on only when the item has been handled and its thread has had time to become idle again.
            handled.wait(timeout=10)
            time.sleep(0.01)

    stats = _run_pipeline(generators=[listing()], handler=handler, concurrency=lambda: 8)
    assert stats.completed == 30
    assert len(threads) == 1, sorted(threads)


def test_live_limit_is_announced_and_each_change_logged_once(caplog: pytest.LogCaptureFixture):
    limit = [2]
    handlers = _HeldHandlers()
    results = []
    caplog.set_level(logging.INFO, logger="bdfr.concurrency")
    thread = _start_pipeline(results, generators=[iter(range(20))], handler=handlers, concurrency=lambda: limit[0])
    try:
        assert handlers.wait_for_running(2)
        limit[0] = 4
        assert handlers.wait_for_running(4)
        _give_the_gate_time_to_misbehave()
    finally:
        handlers.let_finish(20)
        thread.join(timeout=60)
    assert not thread.is_alive()
    assert [r.getMessage() for r in caplog.records if r.name == "bdfr.concurrency"] == [
        "Starting with 2 concurrent workers, adjustable during the run",
        "Concurrent workers changed from 2 to 4",
    ]


@pytest.mark.parametrize(
    ("limit", "in_force"),
    ((0, 1), (-2, 1), (MAX_CONCURRENCY + 100, MAX_CONCURRENCY)),
)
def test_live_limit_outside_its_range_is_announced_as_the_limit_in_force(
    limit: int, in_force: int, caplog: pytest.LogCaptureFixture
):
    """The job's "Latest activity" in the window shows this line, so it has to say what the job really does."""
    current = [3]
    handlers = _HeldHandlers()
    results = []
    caplog.set_level(logging.INFO, logger="bdfr.concurrency")
    thread = _start_pipeline(results, generators=[iter(range(80))], handler=handlers, concurrency=lambda: current[0])
    try:
        assert handlers.wait_for_running(3)
        current[0] = limit
        if in_force > 3:
            assert handlers.wait_for_running(in_force), f"{handlers.running} handlers running"
        else:
            _give_the_gate_time_to_misbehave()
    finally:
        handlers.let_finish(80)
        thread.join(timeout=60)
    assert not thread.is_alive()
    assert [r.getMessage() for r in caplog.records if r.name == "bdfr.concurrency"] == [
        "Starting with 3 concurrent workers, adjustable during the run",
        f"Concurrent workers changed from 3 to {in_force}",
    ]
