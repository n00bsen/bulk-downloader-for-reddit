#!/usr/bin/env python3

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yt_dlp

from bdfr.exceptions import NotADownloadableLinkError, SiteDownloaderError
from bdfr.resource import Resource
from bdfr.site_downloaders.youtube import Youtube

POST_URL = "https://www.youtube.com/watch?v=uSm2VDgRIUs"
MANIFEST_URL = "https://v.redd.it/abc123/DASHPlaylist.mpd?a=1%2Csig&v=1&f=sd"


class FakeYtdlp:
    """Stands in for `yt_dlp.YoutubeDL`, recording each call and writing the file yt-dlp would.

    The written file holds the URL it was "downloaded" from, so a test can tell
    which URL produced the content. `errors` maps a method name to the error it
    raises instead.
    """

    def __init__(self):
        self.calls: list[tuple] = []
        self.errors: dict[str, Exception] = {}
        self.write_output = True

    def __call__(self, options: dict) -> "FakeYtdlp":
        self.options = options
        return self

    def __enter__(self) -> "FakeYtdlp":
        return self

    def __exit__(self, *exc_info) -> bool:
        return False

    def download(self, urls: list[str]) -> int:
        self.calls.append(("download", urls))
        self._raise_if_set("download")
        self._write(urls[0])
        return 0

    def extract_info(self, url: str, download: bool = True, process: bool = True) -> dict:
        self.calls.append(("extract_info", url, download, process))
        self._raise_if_set("extract_info")
        return {"id": "clip", "ext": "mp4", "webpage_url": url, "formats": [{"format_id": "dash-1", "url": url}]}

    def process_ie_result(self, info: dict, download: bool = True) -> dict:
        self.calls.append(("process_ie_result", info, download))
        self._raise_if_set("process_ie_result")
        self._write(info["webpage_url"])
        return info

    def _raise_if_set(self, method: str) -> None:
        if method in self.errors:
            raise self.errors[method]

    def _write(self, url: str) -> None:
        if self.write_output:
            Path(self.options["outtmpl"].replace("%(ext)s", "mp4")).write_bytes(url.encode())


@pytest.fixture()
def fake_ytdlp(monkeypatch: pytest.MonkeyPatch) -> FakeYtdlp:
    fake = FakeYtdlp()
    monkeypatch.setattr(yt_dlp, "YoutubeDL", fake)
    return fake


def _post() -> MagicMock:
    post = MagicMock()
    post.url = POST_URL
    return post


def test_download_video_defaults_to_post_url(fake_ytdlp: FakeYtdlp):
    content = Youtube(_post())._download_video({})({})
    assert content == POST_URL.encode()
    assert fake_ytdlp.calls == [("download", [POST_URL])]


def test_download_video_uses_explicit_url(fake_ytdlp: FakeYtdlp):
    content = Youtube(_post())._download_video({}, MANIFEST_URL)({})
    assert content == MANIFEST_URL.encode()
    assert fake_ytdlp.calls == [("download", [MANIFEST_URL])]


def test_download_video_offers_extra_formats(fake_ytdlp: FakeYtdlp):
    extra = {"format_id": "fallback", "url": "https://v.redd.it/abc123/CMAF_1080.mp4", "acodec": "none"}
    content = Youtube(_post())._download_video({}, MANIFEST_URL, [extra])({})
    assert content == MANIFEST_URL.encode()
    assert fake_ytdlp.calls[0] == ("extract_info", MANIFEST_URL, False, False)
    method, info, download = fake_ytdlp.calls[1]
    assert (method, download) == ("process_ie_result", True)
    assert [f["format_id"] for f in info["formats"]] == ["dash-1", "fallback"]
    assert len(fake_ytdlp.calls) == 2


@pytest.mark.parametrize(
    ("failing_method", "error", "extra_formats"),
    (
        ("download", yt_dlp.utils.DownloadError("ERROR: HTTP Error 403"), ()),
        ("extract_info", yt_dlp.utils.DownloadError("ERROR: HTTP Error 403"), ({"format_id": "x", "url": "u"},)),
        ("process_ie_result", yt_dlp.utils.ExtractorError("Requested format is not available"), ({"url": "u"},)),
        ("process_ie_result", yt_dlp.utils.UnavailableVideoError("connection reset"), ({"url": "u"},)),
    ),
)
def test_download_video_reports_ytdlp_errors_as_site_errors(
    fake_ytdlp: FakeYtdlp, failing_method: str, error: Exception, extra_formats: tuple
):
    fake_ytdlp.errors[failing_method] = error
    download = Youtube(_post())._download_video({}, MANIFEST_URL, extra_formats)
    with pytest.raises(SiteDownloaderError):
        download({})


def test_download_video_without_output_raises(fake_ytdlp: FakeYtdlp):
    fake_ytdlp.write_output = False
    with pytest.raises(NotADownloadableLinkError, match="abc123"):
        Youtube(_post())._download_video({}, MANIFEST_URL)({})


@pytest.mark.online
@pytest.mark.slow
@pytest.mark.parametrize(
    ("test_url", "expected_hash"),
    (
        ("https://www.youtube.com/watch?v=uSm2VDgRIUs", "2d60b54582df5b95ec72bb00b580d2ff"),
        ("https://www.youtube.com/watch?v=GcI7nxQj7HA", "5db0fc92a0a7fb9ac91e63505eea9cf0"),
    ),
)
def test_find_resources_good(test_url: str, expected_hash: str):
    test_submission = MagicMock()
    test_submission.url = test_url
    downloader = Youtube(test_submission)
    resources = downloader.find_resources()
    assert len(resources) == 1
    assert isinstance(resources[0], Resource)
    resources[0].download()
    assert resources[0].hash.hexdigest() == expected_hash


@pytest.mark.online
@pytest.mark.parametrize(
    "test_url",
    (
        "https://www.polygon.com/disney-plus/2020/5/14/21249881/gargoyles-animated-series-disney-plus-greg-weisman"
        "-interview-oj-simpson-goliath-chronicles",
    ),
)
def test_find_resources_bad(test_url: str):
    test_submission = MagicMock()
    test_submission.url = test_url
    downloader = Youtube(test_submission)
    with pytest.raises(NotADownloadableLinkError):
        downloader.find_resources()
