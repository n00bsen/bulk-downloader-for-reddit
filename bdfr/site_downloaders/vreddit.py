#!/usr/bin/env python3

import logging
import urllib.parse
from collections.abc import Callable

from praw.models import Submission

from bdfr.exceptions import NotADownloadableLinkError, SiteDownloaderError
from bdfr.resource import Resource
from bdfr.site_authenticator import SiteAuthenticator
from bdfr.site_downloaders.youtube import Youtube

logger = logging.getLogger(__name__)

# Keys of a listing's `reddit_video` object that hold stream manifests, in the
# order they are tried. The DASH manifest names whole video and audio files;
# HLS names the same files but has them fetched in byte-range pieces.
MANIFEST_KEYS = ("dash_url", "hls_url")


class VReddit(Youtube):
    """Downloads v.redd.it videos from the stream manifests named in the post's listing data.

    Running yt-dlp on the post URL instead has its Reddit extractor fetch that
    same data again, anonymously, from old.reddit.com and www.reddit.com, which
    Reddit can refuse to logged-out clients. It also took a second extraction
    up front just to learn the file extension: four reddit.com requests per
    video, and two even for a video already on disk.
    """

    def __init__(self, post: Submission):
        super().__init__(post)

    def find_resources(self, authenticator: SiteAuthenticator | None = None) -> list[Resource]:
        reddit_video = self._find_reddit_video(self.post)
        manifest_urls = self._get_manifest_urls(reddit_video) if reddit_video else []
        if manifest_urls:
            download_function = self._download_first_available(manifest_urls, self._fallback_formats(reddit_video))
            return [Resource(self.post, self.post.url, download_function, "mp4")]
        ytdl_options = {
            "playlistend": 1,
            "nooverwrites": True,
        }
        download_function = self._download_video(ytdl_options)
        extension = self.get_video_attributes(self.post.url)["ext"]
        res = Resource(self.post, self.post.url, download_function, extension)
        return [res]

    def _download_first_available(self, manifest_urls: list[str], extra_formats: list[dict]) -> Callable:
        """Return a download function that tries each manifest in turn until one yields the video."""
        # Merging into mp4 keeps the file true to the extension declared without an extraction.
        downloads = [
            (url, self._download_video({"nooverwrites": True, "merge_output_format": "mp4"}, url, extra_formats))
            for url in manifest_urls
        ]

        def download(download_parameters: dict) -> bytes:
            *earlier, (_, last_download) = downloads
            for url, download_function in earlier:
                try:
                    return download_function(download_parameters)
                except SiteDownloaderError as e:
                    # Debug only: if the last stream fails too, the downloader reports that error.
                    stream = urllib.parse.urlsplit(url).path
                    logger.debug(f"Stream {stream} failed for {self.post.url}, trying the next one: {e}")
            return last_download(download_parameters)

        return download

    @staticmethod
    def _get_manifest_urls(reddit_video: dict) -> list[str]:
        return [url for key in MANIFEST_KEYS if isinstance(url := reddit_video.get(key), str) and url]

    @staticmethod
    def _fallback_formats(reddit_video: dict) -> list[dict]:
        """Describe the listing's `fallback_url` as a video-only yt-dlp format.

        The manifest URLs Reddit hands out carry `f=sd`, which leaves the top
        resolution out of them; the fallback file is that resolution, without
        audio. yt-dlp's Reddit extractor adds it the same way, and yt-dlp merges
        it with the manifest's audio.
        """
        fallback_url = reddit_video.get("fallback_url")
        if not isinstance(fallback_url, str) or not fallback_url:
            return []

        def number(key: str) -> int | None:
            value = reddit_video.get(key)
            return value if isinstance(value, int) and not isinstance(value, bool) else None

        return [
            {
                "format_id": "fallback",
                "url": fallback_url,
                "ext": "mp4",
                "acodec": "none",
                "width": number("width"),
                "height": number("height"),
                "tbr": number("bitrate_kbps"),
            }
        ]

    @staticmethod
    def _find_reddit_video(post: Submission) -> dict | None:
        """Return the `reddit_video` object of the post, or of the post it crossposts.

        The listing data is read from the instance dict, not as attributes: a
        listing Submission fetches itself from Reddit when a missing attribute
        is read, and most posts lack some of these keys. That would spend an API
        request per post, on a download worker, for data the listing already
        settled.
        """
        post_data = vars(post)
        parents = post_data.get("crosspost_parent_list")
        for data in (post_data, *(parents if isinstance(parents, list) else ())):
            if not isinstance(data, dict):
                continue
            for key in ("secure_media", "media"):
                media = data.get(key)
                if isinstance(media, dict) and isinstance(media.get("reddit_video"), dict):
                    return media["reddit_video"]
        return None

    @staticmethod
    def get_video_attributes(url: str) -> dict:
        result = VReddit.get_video_data(url)
        if "ext" in result:
            return result
        else:
            try:
                result = result["entries"][0]
                return result
            except Exception as e:
                logger.exception(e)
                raise NotADownloadableLinkError(f"Video info extraction failed for {url}") from e
