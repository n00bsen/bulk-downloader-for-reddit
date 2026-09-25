#!/usr/bin/env python3

import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock

import pytest
import requests

from bdfr import http_session
from bdfr.constants import REQUEST_TIMEOUT
from bdfr.exceptions import ResourceNotFound, SiteDownloaderError
from bdfr.site_downloaders.base_downloader import BaseDownloader
from bdfr.site_downloaders.gallery import Gallery
from bdfr.site_downloaders.vidble import Vidble


@pytest.fixture(autouse=True)
def fresh_session() -> Iterator[None]:
    http_session.close_session()
    yield
    http_session.close_session()


class _RecordingHandler(BaseHTTPRequestHandler):
    """Answers every GET with a small body and records which connection carried it."""

    protocol_version = "HTTP/1.1"  # keep-alive, so a client can reuse the connection

    def do_GET(self):
        self.server.seen.append({"client_port": self.client_address[1], "cookie": self.headers.get("Cookie")})
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Set-Cookie", "tracker=1; Path=/")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture()
def local_server(monkeypatch: pytest.MonkeyPatch) -> Iterator[ThreadingHTTPServer]:
    # A system proxy must not intercept requests to the loopback test server.
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RecordingHandler)
    # Handler threads sit on kept-alive connections; closing must not wait for them.
    server.block_on_close = False
    server.seen = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    http_session.close_session()
    server.shutdown()
    server.server_close()


def _server_url(server: ThreadingHTTPServer) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}/image.jpg"


def test_session_reused_within_thread():
    assert http_session.get_session() is http_session.get_session()


def test_session_is_per_thread():
    sessions = []

    def record():
        sessions.append(http_session.get_session())
        sessions.append(http_session.get_session())
        http_session.close_session()

    threads = [threading.Thread(target=record) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    main_session = http_session.get_session()
    # Each thread saw one session twice; no two threads, nor the main thread, shared one.
    assert all(sessions[i] is sessions[i + 1] for i in range(0, len(sessions), 2))
    distinct = {id(s) for s in sessions[::2]} | {id(main_session)}
    assert len(distinct) == 4


def test_close_session_starts_fresh():
    first = http_session.get_session()
    http_session.close_session()
    assert http_session.get_session() is not first


def test_close_session_without_session_is_harmless():
    http_session.close_session()
    http_session.close_session()


def test_adapter_keeps_requests_default_of_no_retries():
    adapter = http_session.get_session().get_adapter("https://i.redd.it/example.jpg")
    # Retries belong to the callers (Resource.http_download backs off itself); an
    # adapter-level retry would silently multiply their waits.
    assert adapter.max_retries.total == 0
    assert adapter._pool_connections == http_session.POOL_CONNECTIONS
    assert adapter._pool_maxsize == http_session.POOL_MAXSIZE


def test_session_reuses_connection(local_server: ThreadingHTTPServer):
    session = http_session.get_session()
    for _ in range(3):
        assert session.get(_server_url(local_server), timeout=REQUEST_TIMEOUT).content == b"ok"
    assert len({request["client_port"] for request in local_server.seen}) == 1


def test_bare_requests_open_a_connection_per_call(local_server: ThreadingHTTPServer):
    # The behaviour the shared session replaces, kept as a baseline for the test above.
    for _ in range(3):
        requests.get(_server_url(local_server), timeout=REQUEST_TIMEOUT)
    assert len({request["client_port"] for request in local_server.seen}) == 3


def test_session_does_not_carry_cookies_between_requests(local_server: ThreadingHTTPServer):
    session = http_session.get_session()
    session.get(_server_url(local_server), timeout=REQUEST_TIMEOUT)
    session.get(_server_url(local_server), timeout=REQUEST_TIMEOUT)
    assert [request["cookie"] for request in local_server.seen] == [None, None]
    assert len(session.cookies) == 0


def test_session_still_sends_per_call_cookies(local_server: ThreadingHTTPServer):
    http_session.get_session().get(_server_url(local_server), cookies={"over18": "1"}, timeout=REQUEST_TIMEOUT)
    assert local_server.seen[0]["cookie"] == "over18=1"


@pytest.fixture()
def fake_session(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    session = MagicMock()
    monkeypatch.setattr(http_session, "get_session", lambda: session)
    return session


def test_retrieve_url_uses_shared_session(fake_session: MagicMock):
    fake_session.get.return_value = MagicMock(status_code=200)
    result = BaseDownloader.retrieve_url("https://example.com/page", cookies={"a": "b"}, headers={"c": "d"})
    assert result is fake_session.get.return_value
    fake_session.get.assert_called_once_with(
        "https://example.com/page", cookies={"a": "b"}, headers={"c": "d"}, timeout=REQUEST_TIMEOUT
    )


def test_retrieve_url_keeps_error_types(fake_session: MagicMock):
    fake_session.get.return_value = MagicMock(status_code=404)
    with pytest.raises(ResourceNotFound, match="Server responded with 404 to https://example.com/page"):
        BaseDownloader.retrieve_url("https://example.com/page")
    fake_session.get.side_effect = requests.exceptions.ConnectionError("refused")
    with pytest.raises(SiteDownloaderError, match="Failed to get page https://example.com/page"):
        BaseDownloader.retrieve_url("https://example.com/page")


def test_fetch_url_returns_error_statuses(fake_session: MagicMock):
    fake_session.get.return_value = MagicMock(status_code=401)
    assert BaseDownloader.fetch_url("https://example.com/page").status_code == 401


def test_gallery_probes_use_shared_session(fake_session: MagicMock):
    fake_session.head.side_effect = [MagicMock(status_code=404), MagicMock(status_code=200)]
    result = Gallery._get_links([{"media_id": "abc123"}])
    assert result == ["https://i.redd.it/abc123.png"]
    fake_session.head.assert_called_with("https://i.redd.it/abc123.png", timeout=REQUEST_TIMEOUT)


def test_vidble_page_uses_shared_session(fake_session: MagicMock):
    fake_session.get.return_value = MagicMock(
        text='<div id="ContentPlaceHolder1_divContent"><img src="/pic_med.jpg"></div>'
    )
    result = Vidble.get_links("https://www.vidble.com/show/abc")
    assert result == {"https://www.vidble.com/pic.jpg"}
    fake_session.get.assert_called_once_with("https://www.vidble.com/show/abc", timeout=REQUEST_TIMEOUT)
