#!/usr/bin/env python3

import json
import re
from collections.abc import Iterator
from unittest.mock import MagicMock, Mock, PropertyMock

import pytest
import requests

from bdfr import http_session
from bdfr.constants import REQUEST_TIMEOUT
from bdfr.exceptions import ResourceNotFound, SiteDownloaderError
from bdfr.resource import Resource
from bdfr.site_downloaders import redgifs
from bdfr.site_downloaders.redgifs import Redgifs

AUTH_URL = "https://api.redgifs.com/v2/auth/temporary"
GIF_API = "https://api.redgifs.com/v2/gifs/"
GALLERY_API = "https://api.redgifs.com/v2/gallery/"
HD_URL = "https://thumbs2.redgifs.com/ExampleClip.mp4"
SD_URL = "https://thumbs2.redgifs.com/ExampleClip-mobile.mp4"


def _response(status_code: int = 200, payload: dict | None = None) -> MagicMock:
    response = MagicMock(spec=requests.Response)
    response.status_code = status_code
    response.ok = status_code < 400
    response.text = json.dumps(payload) if payload is not None else ""
    response.__enter__.return_value = response
    response.__exit__.return_value = False
    return response


def _video_record(hd: str = HD_URL, sd: str = SD_URL) -> dict:
    return {"gif": {"type": 1, "urls": {"hd": hd, "sd": sd}, "gallery": None}}


class FakeSession:
    """Serves canned responses by (method, URL) and records every request.

    Each route holds a list of responses; they are served in order and the last
    one repeats, so a route can model "fails once, then succeeds".
    """

    def __init__(self):
        self.routes: dict[tuple[str, str], list] = {}
        self.calls: list[tuple[str, str, dict]] = []
        self.tokens = iter(f"token-{n}" for n in range(1, 100))

    def route(self, method: str, url: str, *responses) -> None:
        self.routes[(method, url)] = list(responses)

    def get(self, url: str, **kwargs):
        return self._serve("GET", url, kwargs)

    def head(self, url: str, **kwargs):
        return self._serve("HEAD", url, kwargs)

    def _serve(self, method: str, url: str, kwargs: dict):
        self.calls.append((method, url, kwargs))
        if (method, url) == ("GET", AUTH_URL) and (method, url) not in self.routes:
            return _response(200, {"token": next(self.tokens)})
        queue = self.routes[(method, url)]
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def requests_to(self, method: str, url: str) -> list[dict]:
        return [kwargs for m, u, kwargs in self.calls if (m, u) == (method, url)]


@pytest.fixture()
def fake_session(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeSession]:
    session = FakeSession()
    monkeypatch.setattr(http_session, "get_session", lambda: session)
    # Fake tokens must neither come from nor leak into the process-wide cache,
    # which the online tests share as a real run would.
    redgifs._token_cache.clear()
    yield session
    redgifs._token_cache.clear()


@pytest.mark.parametrize(
    ("test_url", "expected"),
    (
        ("https://redgifs.com/watch/frighteningvictorioussalamander", "frighteningvictorioussalamander"),
        ("https://www.redgifs.com/watch/genuineprivateguillemot/", "genuineprivateguillemot"),
        ("https://www.redgifs.com/watch/marriedcrushingcob?rel=u%3Akokiri.girl%3Bo%3Arecent", "marriedcrushingcob"),
        ("https://thumbs4.redgifs.com/DismalIgnorantDrongo.mp4", "dismalignorantdrongo"),
        ("https://thumbs4.redgifs.com/DismalIgnorantDrongo-mobile.mp4", "dismalignorantdrongo"),
        ("https://v3.redgifs.com/watch/newilliteratemeerkat#rel=user%3Atastynova", "newilliteratemeerkat"),
    ),
)
def test_get_id(test_url: str, expected: str):
    result = Redgifs._get_id(test_url)
    assert result == expected


def _assert_hd_body_never_read(session: FakeSession):
    for kwargs in session.requests_to("GET", HD_URL):
        assert kwargs.get("stream") is True, "the HD video was fetched with a body-reading GET"


@pytest.mark.parametrize(("hd_status", "expected"), ((200, "ExampleClip.mp4"), (404, "ExampleClip-mobile.mp4")))
def test_get_link_checks_hd_with_head(fake_session: FakeSession, hd_status: int, expected: str):
    fake_session.route("GET", GIF_API + "exampleclip", _response(200, _video_record()))
    fake_session.route("HEAD", HD_URL, _response(hd_status))

    result = Redgifs._get_link("https://www.redgifs.com/watch/exampleclip")

    assert result == {f"https://thumbs4.redgifs.com/{expected}"}
    assert fake_session.requests_to("GET", HD_URL) == []
    (head,) = fake_session.requests_to("HEAD", HD_URL)
    assert head["allow_redirects"] is True
    assert head["timeout"] == REQUEST_TIMEOUT
    assert head["headers"]["Authorization"] == "Bearer token-1"
    assert head["headers"]["referer"] == "https://www.redgifs.com/"


@pytest.mark.parametrize("head_status", (403, 405, 501))
@pytest.mark.parametrize(("get_status", "expected"), ((200, "ExampleClip.mp4"), (404, "ExampleClip-mobile.mp4")))
def test_get_link_falls_back_to_streamed_get_when_head_refused(
    fake_session: FakeSession, head_status: int, get_status: int, expected: str
):
    streamed = _response(get_status)
    type(streamed).content = PropertyMock(side_effect=AssertionError("HD body was read"))
    fake_session.route("GET", GIF_API + "exampleclip", _response(200, _video_record()))
    fake_session.route("HEAD", HD_URL, _response(head_status))
    fake_session.route("GET", HD_URL, streamed)

    result = Redgifs._get_link("https://www.redgifs.com/watch/exampleclip")

    assert result == {f"https://thumbs4.redgifs.com/{expected}"}
    (get,) = fake_session.requests_to("GET", HD_URL)
    assert get["stream"] is True
    assert get["timeout"] == REQUEST_TIMEOUT
    assert get["headers"]["Authorization"] == "Bearer token-1"
    streamed.__exit__.assert_called_once()
    streamed.iter_content.assert_not_called()
    _assert_hd_body_never_read(fake_session)


def test_token_fetched_once_for_several_posts(fake_session: FakeSession):
    for name in ("first", "second", "third"):
        fake_session.route("GET", GIF_API + name, _response(200, _video_record()))
    fake_session.route("HEAD", HD_URL, _response(200))

    for name in ("first", "second", "third"):
        Redgifs._get_link(f"https://www.redgifs.com/watch/{name}")

    assert len(fake_session.requests_to("GET", AUTH_URL)) == 1
    tokens = {kwargs["headers"]["Authorization"] for m, u, kwargs in fake_session.calls if u.startswith(GIF_API)}
    assert tokens == {"Bearer token-1"}
    _assert_hd_body_never_read(fake_session)


@pytest.mark.parametrize("rejection", (401, 403))
def test_rejected_token_refreshed_once_and_retried(fake_session: FakeSession, rejection: int):
    fake_session.route("GET", GIF_API + "first", _response(rejection), _response(200, _video_record()))
    fake_session.route("GET", GIF_API + "second", _response(200, _video_record()))
    fake_session.route("HEAD", HD_URL, _response(200))

    assert Redgifs._get_link("https://www.redgifs.com/watch/first") == {"https://thumbs4.redgifs.com/ExampleClip.mp4"}
    Redgifs._get_link("https://www.redgifs.com/watch/second")

    assert len(fake_session.requests_to("GET", AUTH_URL)) == 2
    first_calls = [kwargs["headers"]["Authorization"] for kwargs in fake_session.requests_to("GET", GIF_API + "first")]
    assert first_calls == ["Bearer token-1", "Bearer token-2"]
    # The refreshed token is what later posts, and the HD check, use.
    assert fake_session.requests_to("GET", GIF_API + "second")[0]["headers"]["Authorization"] == "Bearer token-2"
    assert fake_session.requests_to("HEAD", HD_URL)[0]["headers"]["Authorization"] == "Bearer token-2"


def test_repeated_rejection_is_reported_without_looping(fake_session: FakeSession):
    fake_session.route("GET", GIF_API + "first", _response(401))

    with pytest.raises(ResourceNotFound, match=f"Server responded with 401 to {GIF_API}first"):
        Redgifs._get_link("https://www.redgifs.com/watch/first")

    assert len(fake_session.requests_to("GET", GIF_API + "first")) == 2
    assert len(fake_session.requests_to("GET", AUTH_URL)) == 2


def test_missing_gif_is_not_treated_as_token_rejection(fake_session: FakeSession):
    fake_session.route("GET", GIF_API + "gone", _response(404))

    with pytest.raises(ResourceNotFound, match=f"Server responded with 404 to {GIF_API}gone"):
        Redgifs._get_link("https://www.redgifs.com/watch/gone")

    assert len(fake_session.requests_to("GET", AUTH_URL)) == 1


def test_empty_token_raises(fake_session: FakeSession):
    fake_session.route("GET", AUTH_URL, _response(200, {"token": ""}))
    with pytest.raises(SiteDownloaderError, match="Unable to retrieve Redgifs API token"):
        Redgifs._get_link("https://www.redgifs.com/watch/first")


def test_image_gallery_links_collected(fake_session: FakeSession):
    record = {"gif": {"type": 2, "urls": {"hd": "https://thumbs3.redgifs.com/Cover-large.jpg"}, "gallery": "g1"}}
    gallery = {
        "gifs": [
            {"urls": {"hd": "https://thumbs2.redgifs.com/One-large.jpg"}},
            {"urls": {"hd": "https://thumbs3.redgifs.com/Two-large.jpg"}},
        ]
    }
    fake_session.route("GET", GIF_API + "cover", _response(200, record))
    # The gallery endpoint rejects the first token here, to show it shares the refresh handling.
    fake_session.route("GET", GALLERY_API + "g1", _response(401), _response(200, gallery))

    result = Redgifs._get_link("https://www.redgifs.com/watch/cover")

    assert result == {"https://thumbs4.redgifs.com/One-large.jpg", "https://thumbs4.redgifs.com/Two-large.jpg"}
    gallery_calls = [
        kwargs["headers"]["Authorization"] for kwargs in fake_session.requests_to("GET", GALLERY_API + "g1")
    ]
    assert gallery_calls == ["Bearer token-1", "Bearer token-2"]
    assert not any(method == "HEAD" for method, _, _ in fake_session.calls)


def test_unknown_type_raises(fake_session: FakeSession):
    fake_session.route("GET", GIF_API + "odd", _response(200, {"gif": {"type": 3}}))
    with pytest.raises(SiteDownloaderError, match="Failed to find JSON data in page"):
        Redgifs._get_link("https://www.redgifs.com/watch/odd")


def test_token_cache_reuses_a_replacement_made_by_another_thread():
    cache = redgifs._TokenCache()
    fetched = iter(("first", "second", "third"))
    fetch = MagicMock(side_effect=lambda: next(fetched))

    assert cache.get(fetch) == "first"
    assert cache.get(fetch) == "first"
    # Two workers both saw "first" rejected; only the first to ask replaces it.
    assert cache.get(fetch, stale="first") == "second"
    assert cache.get(fetch, stale="first") == "second"
    assert fetch.call_count == 2


@pytest.mark.online
@pytest.mark.parametrize(
    ("test_url", "expected"),
    (
        ("https://redgifs.com/watch/frighteningvictorioussalamander", {"FrighteningVictoriousSalamander.mp4"}),
        ("https://redgifs.com/watch/springgreendecisivetaruca", {"SpringgreenDecisiveTaruca.mp4"}),
        ("https://www.redgifs.com/watch/palegoldenrodrawhalibut", {"PalegoldenrodRawHalibut.mp4"}),
        ("https://redgifs.com/watch/hollowintentsnowyowl", {"HollowIntentSnowyowl-large.jpg"}),
        (
            "https://www.redgifs.com/watch/lustrousstickywaxwing",
            {
                "EntireEnchantingHypsilophodon-large.jpg",
                "FancyMagnificentAdamsstaghornedbeetle-large.jpg",
                "LustrousStickyWaxwing-large.jpg",
                "ParchedWindyArmyworm-large.jpg",
                "ThunderousColorlessErmine-large.jpg",
                "UnripeUnkemptWoodpecker-large.jpg",
            },
        ),
        ("https://www.redgifs.com/watch/genuineprivateguillemot/", {"GenuinePrivateGuillemot.mp4"}),
    ),
)
def test_get_link(test_url: str, expected: set[str]):
    result = Redgifs._get_link(test_url)
    result = list(result)
    patterns = [r"https://thumbs\d\.redgifs\.com/" + e + r".*" for e in expected]
    assert all([re.match(p, r) for p in patterns] for r in result)


@pytest.mark.online
@pytest.mark.parametrize(
    ("test_url", "expected_hashes"),
    (
        ("https://redgifs.com/watch/frighteningvictorioussalamander", {"4007c35d9e1f4b67091b5f12cffda00a"}),
        ("https://redgifs.com/watch/springgreendecisivetaruca", {"8dac487ac49a1f18cc1b4dabe23f0869"}),
        ("https://redgifs.com/watch/leafysaltydungbeetle", {"076792c660b9c024c0471ef4759af8bd"}),
        ("https://www.redgifs.com/watch/palegoldenrodrawhalibut", {"46d5aa77fe80c6407de1ecc92801c10e"}),
        ("https://redgifs.com/watch/hollowintentsnowyowl", {"5ee51fa15e0a58e98f11dea6a6cca771"}),
        (
            "https://www.redgifs.com/watch/lustrousstickywaxwing",
            {
                "b461e55664f07bed8d2f41d8586728fa",
                "30ba079a8ed7d7adf17929dc3064c10f",
                "0d4f149d170d29fc2f015c1121bab18b",
                "53987d99cfd77fd65b5fdade3718f9f1",
                "fb2e7d972846b83bf4016447d3060d60",
                "44fb28f72ec9a5cca63fa4369ab4f672",
            },
        ),
    ),
)
def test_download_resource(test_url: str, expected_hashes: set[str]):
    mock_submission = Mock()
    mock_submission.url = test_url
    test_site = Redgifs(mock_submission)
    results = test_site.find_resources()
    assert all(isinstance(res, Resource) for res in results)
    [res.download() for res in results]
    hashes = {res.hash.hexdigest() for res in results}
    assert hashes == set(expected_hashes)


@pytest.mark.online
@pytest.mark.parametrize(
    ("test_url", "expected_link", "expected_hash"),
    (
        (
            "https://redgifs.com/watch/flippantmemorablebaiji",
            {"FlippantMemorableBaiji-mobile.mp4"},
            {"41a5fb4865367ede9f65fc78736f497a"},
        ),
        (
            "https://redgifs.com/watch/thirstyunfortunatewaterdragons",
            {"thirstyunfortunatewaterdragons-mobile.mp4"},
            {"1a51dad8fedb594bdd84f027b3cbe8af"},
        ),
        (
            "https://redgifs.com/watch/conventionalplainxenopterygii",
            {"conventionalplainxenopterygii-mobile.mp4"},
            {"2e1786b3337da85b80b050e2c289daa4"},
        ),
    ),
)
def test_hd_soft_fail(test_url: str, expected_link: set[str], expected_hash: set[str]):
    link = Redgifs._get_link(test_url)
    link = list(link)
    patterns = [r"https://thumbs\d\.redgifs\.com/" + e + r".*" for e in expected_link]
    assert all([re.match(p, r) for p in patterns] for r in link)
    mock_submission = Mock()
    mock_submission.url = test_url
    test_site = Redgifs(mock_submission)
    results = test_site.find_resources()
    assert all(isinstance(res, Resource) for res in results)
    [res.download() for res in results]
    hashes = {res.hash.hexdigest() for res in results}
    assert hashes == set(expected_hash)
