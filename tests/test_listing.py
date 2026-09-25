#!/usr/bin/env python3

import json
import logging
from collections.abc import Iterator
from unittest.mock import MagicMock, call
from urllib.parse import urlparse

import praw
import praw.models
import prawcore
import pytest
import requests

from bdfr.listing import (
    DEFAULT_RATE_LIMIT_WAIT,
    MAX_ATTEMPTS,
    MAX_RATE_LIMIT_WAIT,
    RATE_LIMIT_MARGIN,
    ResumableListing,
    call_with_retry,
    fullname_of,
    is_rate_limit,
    rate_limit_wait,
    retry_delay,
)


def reddit_response(
    status_code: int,
    headers: dict | None = None,
    body: bytes = b"",
    url: str = "https://oauth.reddit.com/user/alice/submitted/",
) -> requests.Response:
    """A response as prawcore receives it, to build its exceptions exactly the way prawcore does."""
    response = requests.Response()
    response.status_code = status_code
    response.headers.update(headers or {})
    response._content = body
    response.url = url
    return response


RATE_LIMITED_BODY = b'{"message": "Too Many Requests", "error": 429}'


def too_many_requests(headers: dict | None = None) -> prawcore.TooManyRequests:
    return prawcore.TooManyRequests(reddit_response(429, headers, RATE_LIMITED_BODY))


def token_rate_limited(headers: dict | None = None) -> prawcore.ResponseException:
    """A 429 from the token endpoint: prawcore's auth._post raises a bare ResponseException for it."""
    response = reddit_response(429, headers, RATE_LIMITED_BODY, "https://www.reddit.com/api/v1/access_token")
    return prawcore.ResponseException(response)


# Both ways prawcore reports Reddit's 429.
RATE_LIMITS = (
    pytest.param(too_many_requests, id="api-429"),
    pytest.param(token_rate_limited, id="token-429"),
)


TRANSIENT_ERRORS = (
    pytest.param(lambda: prawcore.ServerError(reddit_response(503)), id="503"),
    pytest.param(lambda: prawcore.ServerError(reddit_response(500)), id="500"),
    # prawcore raises a bare ResponseException for a 5xx outside its own table, and from the token endpoint.
    pytest.param(lambda: prawcore.ResponseException(reddit_response(502)), id="bare-502"),
    pytest.param(
        lambda: prawcore.RequestException(requests.exceptions.ConnectionError("reset"), ("GET",), {}),
        id="connection",
    ),
    pytest.param(lambda: prawcore.BadJSON(reddit_response(200, body=b"<html>down</html>")), id="bad-json"),
)

FINAL_ERRORS = (
    pytest.param(lambda: prawcore.NotFound(reddit_response(404)), id="not-found"),
    pytest.param(lambda: prawcore.Forbidden(reddit_response(403)), id="forbidden"),
    pytest.param(
        lambda: prawcore.Redirect(reddit_response(302, {"location": "https://www.reddit.com/subreddits/search.json"})),
        id="redirect",
    ),
    pytest.param(lambda: prawcore.InsufficientScope(reddit_response(403)), id="insufficient-scope"),
    pytest.param(lambda: prawcore.BadRequest(reddit_response(400)), id="bad-request"),
    pytest.param(lambda: prawcore.ResponseException(reddit_response(401)), id="unauthorized"),
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch):
    """Fail any test that would reach Reddit: the shared client ID's quota is precious."""

    def refuse(*_args, **_kwargs):
        raise AssertionError("a test tried to make a real HTTP request")

    monkeypatch.setattr(requests.Session, "request", refuse)


@pytest.fixture()
def reddit() -> praw.Reddit:
    return praw.Reddit(
        client_id="test-client-id",
        client_secret="test-client-secret",
        user_agent="test:bdfr:listing",
        check_for_updates=False,
    )


@pytest.fixture()
def sleep() -> MagicMock:
    return MagicMock()


def make_posts(reddit: praw.Reddit, count: int) -> list[praw.models.Submission]:
    """Listing items as PRAW builds them: unfetched, so touching a missing attribute would call Reddit."""
    return [praw.models.Submission(reddit, id=f"p{index:02d}") for index in range(count)]


def ids(items) -> list[str]:
    return [item.id for item in items]


class FakeListingSource:
    """Pages through `items` the way Reddit does, and fails where a test says so.

    `failures` maps an item's position to the errors raised, one per attempt, when
    that item is about to be fetched.
    """

    def __init__(self, items: list, failures: dict[int, list[Exception]] | None = None):
        self.items = items
        self.failures = {position: list(errors) for position, errors in (failures or {}).items()}
        self.calls: list[dict] = []

    def __call__(self, *, limit: int | None, params: dict) -> Iterator:
        self.calls.append({"limit": limit, "params": dict(params)})
        start = 0
        if "after" in params:
            start = [fullname_of(item) for item in self.items].index(params["after"]) + 1
        return self._generate(start, limit)

    def _generate(self, start: int, limit: int | None) -> Iterator:
        produced = 0
        for position in range(start, len(self.items)):
            if limit is not None and produced >= limit:
                return
            if self.failures.get(position):
                raise self.failures[position].pop(0)
            yield self.items[position]
            produced += 1


@pytest.mark.parametrize("rate_limited", RATE_LIMITS)
def test_resumes_after_a_rate_limit_mid_listing(rate_limited, reddit: praw.Reddit, sleep: MagicMock):
    """The rest of a user's posts used to be abandoned at the first 429."""
    posts = make_posts(reddit, 7)
    source = FakeListingSource(posts, {4: [rate_limited({"x-ratelimit-reset": "37"})]})
    listing = ResumableListing(source, None, "submitted posts of u/alice", sleep=sleep)

    assert ids(listing) == ids(posts)
    assert source.calls == [
        {"limit": None, "params": {}},
        {"limit": None, "params": {"after": "t3_p03"}},
    ]
    sleep.assert_called_once_with(37 + RATE_LIMIT_MARGIN)
    assert listing.yielded == 7
    assert listing.last_fullname == "t3_p06"
    assert listing.incomplete is False


def test_resume_asks_only_for_what_the_limit_still_allows(reddit: praw.Reddit, sleep: MagicMock):
    posts = make_posts(reddit, 10)
    source = FakeListingSource(posts, {4: [too_many_requests()]})

    result = list(ResumableListing(source, 6, "submitted posts of u/alice", sleep=sleep))

    assert ids(result) == ids(posts[:6])
    assert source.calls[1] == {"limit": 2, "params": {"after": "t3_p03"}}


def test_rate_limit_before_the_first_item_starts_again(reddit: praw.Reddit, sleep: MagicMock):
    posts = make_posts(reddit, 3)
    source = FakeListingSource(posts, {0: [too_many_requests()]})

    assert ids(ResumableListing(source, 5, "posts of r/aww", sleep=sleep)) == ids(posts)
    assert source.calls == [{"limit": 5, "params": {}}, {"limit": 5, "params": {}}]
    sleep.assert_called_once_with(DEFAULT_RATE_LIMIT_WAIT)


@pytest.mark.parametrize("rate_limited", RATE_LIMITS)
def test_rate_limit_warning_names_the_listing_the_wait_and_the_resume_point(
    rate_limited, reddit: praw.Reddit, sleep: MagicMock, caplog: pytest.LogCaptureFixture
):
    caplog.set_level(logging.WARNING)
    source = FakeListingSource(make_posts(reddit, 4), {2: [rate_limited({"x-ratelimit-reset": "118"})]})

    list(ResumableListing(source, None, "submitted posts of u/alice", sleep=sleep))

    [record] = caplog.records
    assert record.levelno == logging.WARNING
    message = record.getMessage()
    assert "rate limit" in message
    assert "submitted posts of u/alice" in message
    assert f"{118 + RATE_LIMIT_MARGIN} seconds" in message
    assert "after t3_p01 (2 items so far)" in message


@pytest.mark.parametrize(
    ("headers", "expected"),
    (
        ({"x-ratelimit-reset": "37"}, 37 + RATE_LIMIT_MARGIN),
        ({"x-ratelimit-reset": "0"}, RATE_LIMIT_MARGIN),
        # Reddit's window is ten minutes: anything longer is a bad header, not a reason to hang for hours.
        ({"x-ratelimit-reset": "86400"}, MAX_RATE_LIMIT_WAIT),
        ({"x-ratelimit-reset": "-5"}, RATE_LIMIT_MARGIN),
        ({"retry-after": "12"}, 12 + RATE_LIMIT_MARGIN),
        ({"x-ratelimit-reset": "20", "retry-after": "300"}, 20 + RATE_LIMIT_MARGIN),
        ({"x-ratelimit-reset": "soon"}, DEFAULT_RATE_LIMIT_WAIT),
        ({"x-ratelimit-reset": "nan"}, DEFAULT_RATE_LIMIT_WAIT),
        ({}, DEFAULT_RATE_LIMIT_WAIT),
    ),
)
@pytest.mark.parametrize("rate_limited", RATE_LIMITS)
def test_rate_limit_wait_follows_the_reset_header(rate_limited, headers: dict, expected: float):
    assert rate_limit_wait(rate_limited(headers)) == expected


def test_rate_limit_header_names_are_case_insensitive():
    assert rate_limit_wait(too_many_requests({"X-Ratelimit-Reset": "40"})) == 40 + RATE_LIMIT_MARGIN


def test_rate_limit_wait_copes_with_a_response_without_headers():
    assert rate_limit_wait(prawcore.TooManyRequests(MagicMock(status_code=429, headers={}))) == DEFAULT_RATE_LIMIT_WAIT


@pytest.mark.parametrize("rate_limited", RATE_LIMITS)
def test_a_429_is_a_rate_limit_however_prawcore_raises_it(rate_limited):
    """The token endpoint's 429 used to count as final, so setup dropped the user without waiting at all."""
    error = rate_limited()
    assert is_rate_limit(error)
    assert retry_delay(error, 1) == DEFAULT_RATE_LIMIT_WAIT
    # A rate limit keeps its own wait: it does not grow with the transient backoff.
    assert retry_delay(error, 4) == DEFAULT_RATE_LIMIT_WAIT


def test_other_client_errors_from_the_token_endpoint_are_not_rate_limits():
    error = prawcore.ResponseException(reddit_response(400, url="https://www.reddit.com/api/v1/access_token"))
    assert not is_rate_limit(error)
    assert retry_delay(error, 1) is None


@pytest.mark.parametrize("make_error", TRANSIENT_ERRORS)
def test_transient_errors_back_off_increasingly(make_error, reddit: praw.Reddit, sleep: MagicMock):
    posts = make_posts(reddit, 5)
    source = FakeListingSource(posts, {2: [make_error(), make_error(), make_error()]})

    assert ids(ResumableListing(source, None, "posts of r/aww", sleep=sleep)) == ids(posts)
    assert sleep.call_args_list == [call(15), call(30), call(60)]
    assert all(later["params"] == {"after": "t3_p01"} for later in source.calls[1:])


@pytest.mark.parametrize("make_error", FINAL_ERRORS)
def test_errors_that_waiting_cannot_fix_propagate_unchanged(make_error, reddit: praw.Reddit, sleep: MagicMock):
    posts = make_posts(reddit, 5)
    error = make_error()
    source = FakeListingSource(posts, {2: [error]})
    seen = []

    with pytest.raises(type(error)) as caught:
        for item in ResumableListing(source, None, "posts of r/aww", sleep=sleep):
            seen.append(item)

    assert caught.value is error
    assert ids(seen) == ids(posts[:2])
    sleep.assert_not_called()
    assert len(source.calls) == 1


def test_gives_up_after_a_bounded_number_of_failures_without_raising(
    reddit: praw.Reddit, sleep: MagicMock, caplog: pytest.LogCaptureFixture
):
    posts = make_posts(reddit, 6)
    source = FakeListingSource(posts, {3: [prawcore.ServerError(reddit_response(503)) for _ in range(MAX_ATTEMPTS)]})
    listing = ResumableListing(source, None, "submitted posts of u/alice", sleep=sleep)

    assert ids(listing) == ids(posts[:3])

    assert sleep.call_count == MAX_ATTEMPTS - 1
    assert len(source.calls) == MAX_ATTEMPTS
    assert listing.incomplete is True
    errors = [record for record in caplog.records if record.levelno == logging.ERROR]
    [error] = errors
    message = error.getMessage()
    assert "submitted posts of u/alice" in message
    assert "INCOMPLETE" in message
    assert "only 3 items" in message
    assert "t3_p02" in message


def test_giving_up_is_a_run_summary(reddit: praw.Reddit, sleep: MagicMock, caplog: pytest.LogCaptureFixture):
    """A GUI job's final message keeps run summaries; any other error is replaced by "Finished" at once."""
    source = FakeListingSource(make_posts(reddit, 3), {1: [too_many_requests() for _ in range(MAX_ATTEMPTS)]})

    list(ResumableListing(source, None, "submitted posts of u/alice", sleep=sleep))

    [error] = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert getattr(error, "bdfr_event", None) == "run_summary"
    assert "INCOMPLETE" in error.getMessage()


def test_gives_up_on_a_rate_limit_that_never_lifts(reddit: praw.Reddit, sleep: MagicMock):
    source = FakeListingSource(make_posts(reddit, 3), {1: [too_many_requests() for _ in range(MAX_ATTEMPTS)]})
    listing = ResumableListing(source, None, "submitted posts of u/alice", sleep=sleep)

    assert len(list(listing)) == 1
    assert sleep.call_args_list == [call(DEFAULT_RATE_LIMIT_WAIT)] * (MAX_ATTEMPTS - 1)
    assert listing.incomplete is True


def test_progress_resets_the_failure_count(reddit: praw.Reddit, sleep: MagicMock):
    """Only failures in a row count: a long listing may hit the limit many times over."""
    posts = make_posts(reddit, 8)
    almost_too_many = MAX_ATTEMPTS - 1
    source = FakeListingSource(
        posts,
        {
            1: [too_many_requests() for _ in range(almost_too_many)],
            5: [prawcore.ServerError(reddit_response(502)) for _ in range(almost_too_many)],
        },
    )
    listing = ResumableListing(source, None, "posts of r/aww", sleep=sleep)

    assert ids(listing) == ids(posts)
    assert listing.incomplete is False
    assert sleep.call_count == 2 * almost_too_many
    # The backoff starts afresh too.
    assert sleep.call_args_list[almost_too_many] == call(15)


def test_repeated_items_are_not_progress_so_a_failure_after_them_still_gives_up(
    reddit: praw.Reddit, sleep: MagicMock, caplog: pytest.LogCaptureFixture
):
    """A re-ranked page of repeats used to reset the failure count, so the same failure was retried forever."""
    posts = make_posts(reddit, 3)
    calls = []

    def factory(*, limit: int | None, params: dict) -> Iterator:
        calls.append(dict(params))
        if len(calls) > 2 * MAX_ATTEMPTS:
            raise AssertionError("the listing kept retrying instead of giving up")
        if not params:
            yield from posts
        else:
            # Hot re-ranked: the page after the cursor starts with a post already handed out.
            yield posts[0]
        raise too_many_requests()

    listing = ResumableListing(factory, None, "posts of r/aww", sleep=sleep)

    assert ids(listing) == ids(posts)
    assert listing.incomplete is True
    assert len(calls) == MAX_ATTEMPTS
    assert all(later == {"after": "t3_p02"} for later in calls[1:])
    assert sleep.call_count == MAX_ATTEMPTS - 1
    assert "INCOMPLETE" in caplog.text


def test_an_item_repeated_by_a_resumed_page_is_not_handed_out_twice(reddit: praw.Reddit, sleep: MagicMock):
    """Hot and top listings are re-ranked between requests, so the page after a pause can repeat a post."""
    posts = make_posts(reddit, 5)
    pages = iter([[posts[0], posts[1]], [posts[1], posts[2], posts[3], posts[4]]])

    def factory(*, limit: int | None, params: dict) -> Iterator:
        yield from next(pages)
        if not params:
            raise too_many_requests()

    assert ids(ResumableListing(factory, None, "posts of r/aww", sleep=sleep)) == ids(posts)


def test_limit_zero_asks_reddit_for_nothing(sleep: MagicMock):
    factory = MagicMock()
    assert list(ResumableListing(factory, 0, "posts of r/aww", sleep=sleep)) == []
    factory.assert_not_called()


def test_items_without_ids_cannot_be_resumed_so_the_error_propagates(sleep: MagicMock):
    error = too_many_requests()

    def factory(*, limit: int | None, params: dict) -> Iterator:
        yield from (object(), object())
        raise error

    with pytest.raises(prawcore.TooManyRequests) as caught:
        list(ResumableListing(factory, None, "odd listing", sleep=sleep))
    assert caught.value is error
    sleep.assert_not_called()


def test_fullname_is_computed_without_asking_reddit(reddit: praw.Reddit):
    reddit._core = MagicMock()
    reddit._core.request.side_effect = AssertionError("fetched from Reddit")
    submission = praw.models.Submission(reddit, id="abc123")
    comment = praw.models.Comment(reddit, id="def456")

    assert fullname_of(submission) == "t3_abc123"
    assert fullname_of(comment) == "t1_def456"
    assert submission._fetched is False
    assert fullname_of(reddit.subreddit("aww")) is None
    assert fullname_of(object()) is None
    reddit._core.request.assert_not_called()


@pytest.mark.parametrize("rate_limited", RATE_LIMITS)
def test_call_with_retry_waits_out_a_rate_limit(rate_limited, sleep: MagicMock):
    lookup = MagicMock(side_effect=[rate_limited({"x-ratelimit-reset": "9"}), "found"])

    assert call_with_retry(lookup, "the profile of u/alice", sleep=sleep) == "found"
    sleep.assert_called_once_with(9 + RATE_LIMIT_MARGIN)
    assert lookup.call_count == 2


@pytest.mark.parametrize("make_error", FINAL_ERRORS)
def test_call_with_retry_does_not_retry_final_errors(make_error, sleep: MagicMock):
    error = make_error()
    lookup = MagicMock(side_effect=error)

    with pytest.raises(type(error)) as caught:
        call_with_retry(lookup, "r/aww", sleep=sleep)
    assert caught.value is error
    lookup.assert_called_once_with()
    sleep.assert_not_called()


def test_call_with_retry_leaves_other_exceptions_alone(sleep: MagicMock):
    lookup = MagicMock(side_effect=ValueError("not Reddit's fault"))
    with pytest.raises(ValueError, match="not Reddit's fault"):
        call_with_retry(lookup, "r/aww", sleep=sleep)
    sleep.assert_not_called()


def test_call_with_retry_raises_the_last_error_once_attempts_run_out(sleep: MagicMock):
    errors = [prawcore.ServerError(reddit_response(503)) for _ in range(MAX_ATTEMPTS)]
    lookup = MagicMock(side_effect=errors)

    with pytest.raises(prawcore.ServerError) as caught:
        call_with_retry(lookup, "r/aww", sleep=sleep)
    assert caught.value is errors[-1]
    assert sleep.call_args_list == [call(15), call(30), call(60), call(120)]


class FakeRedditApi:
    """Answer PRAW's HTTP requests with a token and pages of u/alice's submitted posts.

    Only the HTTP layer is fake: PRAW builds the real ListingGenerator, and
    prawcore turns a 429 into TooManyRequests exactly as it does against Reddit.
    """

    def __init__(
        self,
        post_ids: list[str],
        page_size: int,
        rate_limited_requests: set[int],
        token_responses: list[requests.Response] | None = None,
    ):
        self.post_ids = post_ids
        self.page_size = page_size
        self.rate_limited_requests = rate_limited_requests
        # Answers to the first token requests, in order; every later one is granted.
        self.token_responses = list(token_responses or [])
        self.token_requests = 0
        self.listing_requests: list[dict] = []

    def request(self, method: str, url: str, *_args, params=None, **_kwargs) -> requests.Response:
        if urlparse(url).path == "/api/v1/access_token":
            self.token_requests += 1
            if self.token_responses:
                return self.token_responses.pop(0)
            return self.token(3600)
        assert urlparse(url).path.rstrip("/") == "/user/alice/submitted", url
        self.listing_requests.append(dict(params))
        if len(self.listing_requests) in self.rate_limited_requests:
            # Without x-ratelimit-remaining prawcore's own limiter leaves the wait to BDFR.
            return self._json(429, {"message": "Too Many Requests", "error": 429}, {"x-ratelimit-reset": "41"})
        start = 0
        if "after" in params:
            start = self.post_ids.index(params["after"].removeprefix("t3_")) + 1
        page = self.post_ids[start : start + min(int(params["limit"]), self.page_size)]
        after = f"t3_{page[-1]}" if page and start + len(page) < len(self.post_ids) else None
        children = [{"kind": "t3", "data": {"id": post_id, "name": f"t3_{post_id}"}} for post_id in page]
        return self._json(200, {"kind": "Listing", "data": {"after": after, "before": None, "children": children}})

    @staticmethod
    def _json(status_code: int, payload: dict, headers: dict | None = None) -> requests.Response:
        return reddit_response(status_code, headers, json.dumps(payload).encode())

    @classmethod
    def token(cls, expires_in: int) -> requests.Response:
        return cls._json(200, {"access_token": "token", "expires_in": expires_in, "scope": "*", "token_type": "bearer"})


def test_real_praw_listing_resumes_after_a_429(reddit: praw.Reddit, sleep: MagicMock, monkeypatch: pytest.MonkeyPatch):
    post_ids = [f"q{index:02d}" for index in range(8)]
    api = FakeRedditApi(post_ids, page_size=3, rate_limited_requests={2})
    monkeypatch.setattr(requests.Session, "request", lambda _session, *args, **kwargs: api.request(*args, **kwargs))
    listing = ResumableListing(
        reddit.redditor("alice").submissions.new, None, "submitted posts of u/alice", sleep=sleep
    )

    result = list(listing)

    assert [post.id for post in result] == post_ids
    assert all(isinstance(post, praw.models.Submission) for post in result)
    sleep.assert_called_once_with(41 + RATE_LIMIT_MARGIN)
    afters = [request.get("after") for request in api.listing_requests]
    # The rebuilt generator starts from the last post handed out, not from the top.
    assert afters == [None, "t3_q02", "t3_q02", "t3_q05"]
    assert all(request["sort"] == "new" for request in api.listing_requests)


def test_real_praw_listing_resumes_when_the_token_refresh_is_rate_limited(
    reddit: praw.Reddit, sleep: MagicMock, monkeypatch: pytest.MonkeyPatch
):
    """prawcore raises the token endpoint's 429 as a bare ResponseException; it used to abandon the listing."""
    post_ids = [f"q{index:02d}" for index in range(8)]
    # The first token has already expired by the next page, so fetching that page asks for a new one first.
    api = FakeRedditApi(
        post_ids,
        page_size=3,
        rate_limited_requests=set(),
        token_responses=[FakeRedditApi.token(10), token_rate_limited().response],
    )
    monkeypatch.setattr(requests.Session, "request", lambda _session, *args, **kwargs: api.request(*args, **kwargs))
    listing = ResumableListing(
        reddit.redditor("alice").submissions.new, None, "submitted posts of u/alice", sleep=sleep
    )

    assert [post.id for post in listing] == post_ids
    assert listing.incomplete is False
    # The token endpoint's 429 carries no reset header here, so the default wait applies.
    sleep.assert_called_once_with(DEFAULT_RATE_LIMIT_WAIT)
    assert api.token_requests == 3
    assert [request.get("after") for request in api.listing_requests] == [None, "t3_q02", "t3_q05"]
