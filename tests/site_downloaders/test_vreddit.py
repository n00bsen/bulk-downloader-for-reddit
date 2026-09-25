#!/usr/bin/env python3

from pathlib import Path
from unittest.mock import MagicMock, PropertyMock

import praw.models
import pytest
import yt_dlp

from bdfr.exceptions import NotADownloadableLinkError, SiteDownloaderError
from bdfr.resource import Resource
from bdfr.site_downloaders.vreddit import VReddit

POST_URL = "https://v.redd.it/abc123"
DASH_URL = "https://v.redd.it/abc123/DASHPlaylist.mpd?a=1792874921%2Csig%3D%3D&v=1&f=sd"
HLS_URL = "https://v.redd.it/abc123/HLSPlaylist.m3u8?a=1792874921%2Csig%3D%3D&v=1&f=sd"
FALLBACK_URL = "https://v.redd.it/abc123/CMAF_1080.mp4?source=fallback"


class FakeYtdlp:
    """Stands in for `yt_dlp.YoutubeDL`, recording each call and writing the file yt-dlp would.

    The written file holds the URL it came from, so a test can tell which stream
    produced the content. URLs in `failing` raise as a refused manifest does.
    """

    def __init__(self):
        self.calls: list[tuple] = []
        self.options: list[dict] = []
        self.failing: set[str] = set()

    def __call__(self, options: dict) -> "FakeYtdlp":
        self.options.append(options)
        return self

    def __enter__(self) -> "FakeYtdlp":
        return self

    def __exit__(self, *exc_info) -> bool:
        return False

    def download(self, urls: list[str]) -> int:
        self.calls.append(("download", urls[0]))
        self._fetch(urls[0])
        return 0

    def extract_info(self, url: str, download: bool = True, process: bool = True) -> dict:
        self.calls.append(("extract_info", url))
        if url in self.failing:
            raise yt_dlp.utils.DownloadError(f"ERROR: HTTP Error 403: Forbidden for {url}")
        return {"id": "clip", "ext": "mp4", "webpage_url": url, "formats": [{"format_id": "dash-720", "url": url}]}

    def process_ie_result(self, info: dict, download: bool = True) -> dict:
        self.calls.append(("process_ie_result", info["webpage_url"]))
        self.processed = info
        self._fetch(info["webpage_url"])
        return info

    def _fetch(self, url: str) -> None:
        if url in self.failing:
            raise yt_dlp.utils.DownloadError(f"ERROR: HTTP Error 403: Forbidden for {url}")
        Path(self.options[-1]["outtmpl"].replace("%(ext)s", "mp4")).write_bytes(url.encode())

    @property
    def urls(self) -> list[str]:
        return [url for _, url in self.calls]


@pytest.fixture()
def fake_ytdlp(monkeypatch: pytest.MonkeyPatch) -> FakeYtdlp:
    fake = FakeYtdlp()
    monkeypatch.setattr(yt_dlp, "YoutubeDL", fake)
    return fake


def _reddit_video(**overrides) -> dict:
    video = {
        "bitrate_kbps": 5000,
        "dash_url": DASH_URL,
        "duration": 14,
        "fallback_url": FALLBACK_URL,
        "has_audio": True,
        "height": 1920,
        "hls_url": HLS_URL,
        "is_gif": False,
        "transcoding_status": "completed",
        "width": 1080,
    }
    video.update(overrides)
    return {key: value for key, value in video.items() if value is not None}


def _listing_submission(**data) -> praw.models.Submission:
    """A Submission as a listing yields it: unfetched, so reading a missing attribute asks Reddit for the post."""
    submission = praw.models.Submission(MagicMock(), _data={"id": "abc123", "url": POST_URL, **data})
    assert not submission._fetched
    return submission


def _assert_no_reddit_request(submission: praw.models.Submission) -> None:
    submission._reddit.request.assert_not_called()
    assert not submission._fetched


@pytest.mark.parametrize(
    "data",
    (
        {"secure_media": {"reddit_video": _reddit_video()}, "media": {"reddit_video": _reddit_video()}},
        {"secure_media": {"reddit_video": _reddit_video()}},
        {"secure_media": None, "media": {"reddit_video": _reddit_video()}},
        {
            "secure_media": None,
            "media": None,
            "crosspost_parent_list": [{"secure_media": {"reddit_video": _reddit_video()}}],
        },
        {"secure_media": None, "media": None, "crosspost_parent_list": [{"media": {"reddit_video": _reddit_video()}}]},
    ),
    ids=("both", "secure_media", "media", "crosspost_secure_media", "crosspost_media"),
)
def test_listing_video_needs_no_extraction(fake_ytdlp: FakeYtdlp, data: dict):
    submission = _listing_submission(**data)
    resources = VReddit(submission).find_resources()
    assert len(resources) == 1
    assert resources[0].extension == "mp4"
    assert resources[0].url == POST_URL
    assert fake_ytdlp.calls == []
    resources[0].download()
    assert resources[0].content == DASH_URL.encode()
    assert POST_URL not in fake_ytdlp.urls
    _assert_no_reddit_request(submission)


def test_own_video_preferred_over_crosspost_parent(fake_ytdlp: FakeYtdlp):
    parent_dash = "https://v.redd.it/parent/DASHPlaylist.mpd?a=1&v=1&f=sd"
    submission = _listing_submission(
        secure_media={"reddit_video": _reddit_video()},
        crosspost_parent_list=[{"secure_media": {"reddit_video": _reddit_video(dash_url=parent_dash)}}],
    )
    resources = VReddit(submission).find_resources()
    resources[0].download()
    assert resources[0].content == DASH_URL.encode()


def test_dash_manifest_preferred_over_hls(fake_ytdlp: FakeYtdlp):
    resources = VReddit(_listing_submission(secure_media={"reddit_video": _reddit_video()})).find_resources()
    resources[0].download()
    assert fake_ytdlp.urls == [DASH_URL, DASH_URL]
    assert HLS_URL not in fake_ytdlp.urls
    assert fake_ytdlp.options[0]["merge_output_format"] == "mp4"


def test_hls_manifest_used_without_dash(fake_ytdlp: FakeYtdlp):
    submission = _listing_submission(secure_media={"reddit_video": _reddit_video(dash_url=None)})
    resources = VReddit(submission).find_resources()
    resources[0].download()
    assert resources[0].content == HLS_URL.encode()
    assert fake_ytdlp.urls == [HLS_URL, HLS_URL]


def test_hls_manifest_tried_when_dash_fails(fake_ytdlp: FakeYtdlp):
    fake_ytdlp.failing.add(DASH_URL)
    resources = VReddit(_listing_submission(secure_media={"reddit_video": _reddit_video()})).find_resources()
    resources[0].download()
    assert resources[0].content == HLS_URL.encode()
    assert fake_ytdlp.urls == [DASH_URL, HLS_URL, HLS_URL]


def test_failure_of_every_manifest_is_reported(fake_ytdlp: FakeYtdlp):
    fake_ytdlp.failing.update((DASH_URL, HLS_URL))
    resources = VReddit(_listing_submission(secure_media={"reddit_video": _reddit_video()})).find_resources()
    with pytest.raises(SiteDownloaderError, match="HLSPlaylist"):
        resources[0].download()
    assert POST_URL not in fake_ytdlp.urls


def test_fallback_file_offered_as_video_only_format(fake_ytdlp: FakeYtdlp):
    resources = VReddit(_listing_submission(secure_media={"reddit_video": _reddit_video()})).find_resources()
    resources[0].download()
    formats = fake_ytdlp.processed["formats"]
    assert formats[0]["format_id"] == "dash-720"
    assert formats[1] == {
        "format_id": "fallback",
        "url": FALLBACK_URL,
        "ext": "mp4",
        "acodec": "none",
        "width": 1080,
        "height": 1920,
        "tbr": 5000,
    }


def test_manifest_downloaded_as_is_without_fallback_url(fake_ytdlp: FakeYtdlp):
    submission = _listing_submission(secure_media={"reddit_video": _reddit_video(fallback_url=None)})
    resources = VReddit(submission).find_resources()
    resources[0].download()
    assert resources[0].content == DASH_URL.encode()
    assert fake_ytdlp.calls == [("download", DASH_URL)]


@pytest.mark.parametrize(
    "data",
    (
        {},
        {"secure_media": None, "media": None},
        {"secure_media": {"type": "youtube.com", "oembed": {}}, "media": None},
        {"secure_media": {"reddit_video": _reddit_video(dash_url=None, hls_url=None)}},
        {"crosspost_parent_list": []},
        {"crosspost_parent_list": [{"secure_media": None, "media": None}]},
    ),
    ids=("no_media_keys", "none", "other_host", "no_manifests", "empty_parents", "parent_without_video"),
)
def test_post_url_extracted_without_listing_video(fake_ytdlp: FakeYtdlp, data: dict):
    submission = _listing_submission(**data)
    resources = VReddit(submission).find_resources()
    assert fake_ytdlp.calls == [("extract_info", POST_URL)]
    assert resources[0].extension == "mp4"
    resources[0].download()
    assert fake_ytdlp.calls[1:] == [("download", POST_URL)]
    assert fake_ytdlp.options[-1]["playlistend"] == 1
    assert "merge_output_format" not in fake_ytdlp.options[-1]
    _assert_no_reddit_request(submission)


def test_listing_fields_not_read_as_attributes(fake_ytdlp: FakeYtdlp):
    post = MagicMock()
    post.url = POST_URL
    for name in ("secure_media", "media", "crosspost_parent_list"):
        setattr(type(post), name, PropertyMock(side_effect=AssertionError(f"{name} read as an attribute")))
    resources = VReddit(post).find_resources()
    assert fake_ytdlp.calls == [("extract_info", POST_URL)]
    assert len(resources) == 1


@pytest.mark.online
@pytest.mark.slow
@pytest.mark.parametrize(
    ("test_url", "expected_hash"),
    (("https://reddit.com/r/Unexpected/comments/z4xsuj/omg_thats_so_cute/", "1ffab5e5c0cc96db18108e4f37e8ca7f"),),
)
def test_find_resources_good(test_url: str, expected_hash: str):
    test_submission = MagicMock()
    test_submission.url = test_url
    downloader = VReddit(test_submission)
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
    downloader = VReddit(test_submission)
    with pytest.raises(NotADownloadableLinkError):
        downloader.find_resources()
