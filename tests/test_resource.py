#!/usr/bin/env python3

from unittest.mock import MagicMock

import pytest
import requests

from bdfr import http_session
from bdfr.constants import REQUEST_TIMEOUT
from bdfr.exceptions import BulkDownloaderException
from bdfr.resource import Resource


@pytest.fixture()
def fake_session(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    session = MagicMock()
    monkeypatch.setattr(http_session, "get_session", lambda: session)
    return session


@pytest.fixture()
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    recorded = []
    monkeypatch.setattr("bdfr.resource.time.sleep", recorded.append)
    return recorded


def _response(status_code: int, content: bytes = b"") -> MagicMock:
    return MagicMock(status_code=status_code, content=content)


@pytest.mark.parametrize(
    ("test_url", "expected"),
    (
        ("test.png", ".png"),
        ("another.mp4", ".mp4"),
        ("test.jpeg", ".jpeg"),
        ("http://www.random.com/resource.png", ".png"),
        ("https://www.resource.com/test/example.jpg", ".jpg"),
        ("hard.png.mp4", ".mp4"),
        ("https://preview.redd.it/7zkmr1wqqih61.png?width=237&format=png&auto=webp&s=19de214e634cbcad99", ".png"),
        ("test.jpg#test", ".jpg"),
        ("test.jpg?width=247#test", ".jpg"),
        ("https://www.test.com/test/test2/example.png?random=test#thing", ".png"),
    ),
)
def test_resource_get_extension(test_url: str, expected: str):
    test_resource = Resource(MagicMock(), test_url, lambda: None)
    result = test_resource._determine_extension()
    assert result == expected


def test_http_download_uses_shared_session(fake_session: MagicMock, sleeps: list[float]):
    fake_session.get.return_value = _response(200, b"image bytes")
    result = Resource.http_download("https://i.redd.it/a.jpg", {"headers": {"User-Agent": "x"}})
    assert result == b"image bytes"
    fake_session.get.assert_called_once_with(
        "https://i.redd.it/a.jpg", headers={"User-Agent": "x"}, timeout=REQUEST_TIMEOUT
    )
    assert sleeps == []


@pytest.mark.parametrize("status_code", (408, 429))
def test_http_download_retries_transient_status(fake_session: MagicMock, sleeps: list[float], status_code: int):
    fake_session.get.side_effect = [_response(status_code), _response(200, b"data")]
    assert Resource.http_download("https://i.redd.it/a.jpg", {}) == b"data"
    assert fake_session.get.call_count == 2
    assert sleeps == [60]


@pytest.mark.parametrize(
    "error",
    (
        requests.exceptions.ConnectionError("reset"),
        requests.exceptions.ChunkedEncodingError("truncated"),
        requests.exceptions.Timeout("slow"),
    ),
)
def test_http_download_retries_network_errors(fake_session: MagicMock, sleeps: list[float], error: Exception):
    fake_session.get.side_effect = [error, _response(200, b"data")]
    assert Resource.http_download("https://i.redd.it/a.jpg", {}) == b"data"
    assert sleeps == [60]


def test_http_download_backs_off_until_max_wait_time(fake_session: MagicMock, sleeps: list[float]):
    fake_session.get.side_effect = requests.exceptions.ConnectionError("down")
    with pytest.raises(requests.exceptions.ConnectionError):
        Resource.http_download("https://i.redd.it/a.jpg", {"max_wait_time": 180})
    assert sleeps == [60, 120, 180]


def test_http_download_unrecoverable_status_is_not_retried(fake_session: MagicMock, sleeps: list[float]):
    fake_session.get.return_value = _response(404)
    with pytest.raises(BulkDownloaderException, match="HTTP Code 404"):
        Resource.http_download("https://i.redd.it/a.jpg", {})
    assert fake_session.get.call_count == 1
    assert sleeps == []


def test_download_wraps_exhausted_connection_errors(fake_session: MagicMock, sleeps: list[float]):
    fake_session.get.side_effect = requests.exceptions.ConnectionError("down")
    resource = Resource(MagicMock(), "https://i.redd.it/a.jpg", Resource.retry_download("https://i.redd.it/a.jpg"))
    with pytest.raises(BulkDownloaderException, match="Could not download resource"):
        resource.download({"max_wait_time": 60})
    assert sleeps == [60]


@pytest.mark.online
@pytest.mark.parametrize(
    ("test_url", "expected_hash"),
    (("https://www.iana.org/_img/2013.1/iana-logo-header.svg", "426b3ac01d3584c820f3b7f5985d6623"),),
)
def test_download_online_resource(test_url: str, expected_hash: str):
    test_resource = Resource(MagicMock(), test_url, Resource.retry_download(test_url))
    test_resource.download()
    assert test_resource.hash.hexdigest() == expected_hash
