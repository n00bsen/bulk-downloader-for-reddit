#!/usr/bin/env python3

"""Reddit listings that survive rate limits and brief outages.

The client ID bundled with BDFR is shared by every user, so its quota regularly
runs out. prawcore does not retry a 429 (TooManyRequests), and a PRAW listing
that raised mid-iteration used to end right there: the consumer logged one
error and moved on to the next source, so the rest of a user's posts were
silently skipped while the job still "completed".

ResumableListing waits for the rate-limit window to reset and carries on from
the last item it handed out, using Reddit's own "after" cursor, so nothing is
fetched twice. call_with_retry gives the setup lookups (does this user exist, is
this subreddit reachable) the same treatment, so a rate limit during setup no
longer drops a source either.
"""

import logging
import math
import time
from collections.abc import Callable, Iterable, Iterator
from typing import Any, Generic, TypeVar

import prawcore

logger = logging.getLogger(__name__)

T = TypeVar("T")

Sleep = Callable[[float], Any]

# Seconds to wait after a 429 that does not say when the limit resets. It matches
# the pause BDFR has always taken after a failed listing.
DEFAULT_RATE_LIMIT_WAIT = 60

# Reddit's rate-limit window is ten minutes long, so no reset is ever further
# away than that; a larger value in a header can only be wrong.
MAX_RATE_LIMIT_WAIT = 600

# x-ratelimit-reset is a whole number of seconds, rounded down, and Reddit keeps
# answering 429 with a reset of 0 for a moment after it: close to a second when
# this was measured. Waiting a little past it avoids burning an attempt there.
RATE_LIMIT_MARGIN = 5

# prawcore has already retried a 5xx or a dropped connection three times within a
# few seconds before it raises, so what reaches BDFR is an outage lasting at least
# that long. The backoff starts well above that and doubles from there.
TRANSIENT_BACKOFF_BASE = 15
TRANSIENT_BACKOFF_MAX = 240

# Failed attempts in a row, without a single item in between, before giving up.
MAX_ATTEMPTS = 5


def _seconds(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            return None
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return max(float(value), 0.0)


def is_rate_limit(error: Exception) -> bool:
    """Whether `error` is Reddit's 429, however prawcore happened to raise it.

    API requests raise TooManyRequests, but the token endpoint, asked for a first
    token or a refreshed one, raises a bare ResponseException for every status. A
    GUI job is a fresh process that asks for a token first, so that 429 is common
    when several jobs start together on the shared client ID.
    """
    if isinstance(error, prawcore.TooManyRequests):
        return True
    return isinstance(error, prawcore.ResponseException) and getattr(error.response, "status_code", None) == 429


def rate_limit_wait(error: prawcore.ResponseException) -> float:
    """Seconds until Reddit's rate-limit window resets, as the 429 response reports it."""
    headers = getattr(error.response, "headers", None) or {}
    for header in ("x-ratelimit-reset", "retry-after"):
        seconds = _seconds(headers.get(header))
        if seconds is not None:
            return min(seconds + RATE_LIMIT_MARGIN, MAX_RATE_LIMIT_WAIT)
    return DEFAULT_RATE_LIMIT_WAIT


def _is_transient(error: prawcore.PrawcoreException) -> bool:
    # BadJSON is a 200 whose body is not JSON, typically an HTML maintenance page.
    if isinstance(error, (prawcore.ServerError, prawcore.RequestException, prawcore.BadJSON)):
        return True
    if isinstance(error, prawcore.ResponseException):
        # The token endpoint reports any failure, a 5xx included, as a bare ResponseException.
        status = getattr(error.response, "status_code", None)
        return isinstance(status, int) and 500 <= status < 600
    return False


def retry_delay(error: Exception, failures: int) -> float | None:
    """Seconds to wait before trying again after `error`, or None when waiting cannot help.

    `failures` counts the failed attempts in a row, this one included, and sets the
    backoff for a transient error. A missing, private or banned source, and refused
    credentials, are all final.
    """
    if is_rate_limit(error):
        return rate_limit_wait(error)
    if isinstance(error, prawcore.PrawcoreException) and _is_transient(error):
        return min(TRANSIENT_BACKOFF_BASE * 2 ** (failures - 1), TRANSIENT_BACKOFF_MAX)
    return None


def _warn_retry(description: str, error: Exception, wait: float, failures: int, max_attempts: int, then: str) -> None:
    attempt = f"attempt {failures + 1} of {max_attempts}"
    if is_rate_limit(error):
        logger.warning(
            f"Reddit's API rate limit was reached while reading {description}. "
            f"Waiting {wait:.0f} seconds for it to reset, then {then} ({attempt})"
        )
    else:
        logger.warning(
            f"Reddit failed while reading {description} ({error}). Waiting {wait:.0f} seconds, then {then} ({attempt})"
        )


def fullname_of(item: object) -> str | None:
    """Return a listing item's fullname, such as "t3_abc123": the cursor Reddit pages by.

    Only the instance dictionary is read. A listing item that lacks an attribute
    fetches itself from Reddit when it is touched, and this must never cost a
    request. `_kind` is safe: it is a lookup in PRAW's configuration, and PRAW
    never fetches for an underscored name.
    """
    item_id = (getattr(item, "__dict__", None) or {}).get("id")
    if not isinstance(item_id, str) or not item_id:
        return None
    if "_" in item_id:
        return item_id
    kind = getattr(item, "_kind", None)
    return f"{kind}_{item_id}" if isinstance(kind, str) and kind else None


def call_with_retry(
    function: Callable[[], T], description: str, *, sleep: Sleep = time.sleep, max_attempts: int = MAX_ATTEMPTS
) -> T:
    """Call a setup lookup, waiting out rate limits and brief outages.

    An error that retrying cannot fix propagates at once, and so does the last
    error once `max_attempts` calls in a row have failed, so the caller reports
    it exactly as before.
    """
    failures = 0
    while True:
        try:
            return function()
        except prawcore.PrawcoreException as error:
            failures += 1
            wait = retry_delay(error, failures)
            if wait is None or failures >= max_attempts:
                raise
            _warn_retry(description, error, wait, failures, max_attempts, "trying again")
            sleep(wait)


class ResumableListing(Generic[T]):
    """A PRAW listing that resumes where it stopped after a rate limit or an outage.

    `factory` builds the underlying ListingGenerator and is called as
    `factory(limit=..., params=...)`, which every PRAW listing method accepts. After
    a failure it is called again with `params={"after": <last fullname>}` and the
    limit reduced by the items already yielded, so the listing continues rather
    than starting over. Errors that retrying cannot fix propagate unchanged. After
    `max_attempts` failures in a row it logs that the listing is incomplete and
    stops, so that the consumer moves on to the next source.
    """

    def __init__(
        self,
        factory: Callable[..., Iterable[T]],
        limit: int | None,
        description: str,
        *,
        sleep: Sleep = time.sleep,
        max_attempts: int = MAX_ATTEMPTS,
    ):
        self.factory = factory
        self.limit = limit
        self.description = description
        self._sleep = sleep
        self._max_attempts = max_attempts
        self.yielded = 0
        self.last_fullname: str | None = None
        self.incomplete = False
        # Reddit re-ranks hot and top listings between requests, so a resumed page
        # can repeat an item that was already handed out.
        self._seen: set[str] = set()

    def __repr__(self) -> str:
        return f"ResumableListing({self.description!r}, limit={self.limit})"

    def _build(self) -> Iterator[T]:
        params = {"after": self.last_fullname} if self.last_fullname else {}
        limit = None if self.limit is None else self.limit - self.yielded
        return iter(self.factory(limit=limit, params=params))

    def __iter__(self) -> Iterator[T]:
        generator = None
        failures = 0
        while self.limit is None or self.yielded < self.limit:
            try:
                if generator is None:
                    generator = self._build()
                item = next(generator)
            except StopIteration:
                return
            except prawcore.PrawcoreException as error:
                failures += 1
                wait = retry_delay(error, failures)
                # Items without an ID leave no cursor to resume from, and starting
                # over would hand out every one of them again.
                if wait is None or (self.yielded and self.last_fullname is None):
                    raise
                if failures >= self._max_attempts:
                    self._give_up(error, failures)
                    return
                if self.last_fullname:
                    then = f"resuming after {self.last_fullname} ({self.yielded} items so far)"
                else:
                    then = "starting it again"
                _warn_retry(self.description, error, wait, failures, self._max_attempts, then)
                self._sleep(wait)
                generator = None
                continue
            fullname = fullname_of(item)
            if fullname is not None:
                if fullname in self._seen:
                    continue
                self._seen.add(fullname)
                self.last_fullname = fullname
            # Only a new item is progress. A repeated one leaves the cursor where it was,
            # so counting it would let a resumed page of repeats retry the same failure forever.
            failures = 0
            self.yielded += 1
            yield item

    def _give_up(self, error: Exception, failures: int) -> None:
        self.incomplete = True
        last = f", the last being {self.last_fullname}" if self.last_fullname else ""
        # A run summary, because a GUI job's last message otherwise replaces this
        # with "Finished" and the job looks complete.
        logger.error(
            f"Giving up on {self.description} after {failures} failed attempts in a row ({error}). "
            f"The listing is INCOMPLETE: only {self.yielded} items were retrieved{last}.",
            extra={"bdfr_event": "run_summary"},
        )
