#!/usr/bin/env python3

import logging
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path

import yt_dlp
from praw.models import Submission

from bdfr.exceptions import NotADownloadableLinkError, SiteDownloaderError
from bdfr.resource import Resource
from bdfr.site_authenticator import SiteAuthenticator
from bdfr.site_downloaders.base_downloader import BaseDownloader

logger = logging.getLogger(__name__)


class Youtube(BaseDownloader):
    def __init__(self, post: Submission):
        super().__init__(post)

    def find_resources(self, authenticator: SiteAuthenticator | None = None) -> list[Resource]:
        ytdl_options = {
            "format": "best",
            "playlistend": 1,
            "nooverwrites": True,
        }
        download_function = self._download_video(ytdl_options)
        extension = self.get_video_attributes(self.post.url)["ext"]
        res = Resource(self.post, self.post.url, download_function, extension)
        return [res]

    def _download_video(
        self, ytdl_options: dict, url: str | None = None, extra_formats: Sequence[dict] = ()
    ) -> Callable:
        """Return a download function that fetches `url` (the post's URL by default) with yt-dlp.

        Subclasses pass `url` when they know a better address for the media
        than the post link, such as a stream manifest, and `extra_formats` for
        streams they know of that the URL's extraction does not list. yt-dlp
        then chooses among all of them as usual.
        """
        yt_logger = logging.getLogger("youtube-dl")
        yt_logger.setLevel(logging.CRITICAL)
        ytdl_options["quiet"] = True
        ytdl_options["logger"] = yt_logger

        def download(_: dict) -> bytes:
            target_url = self.post.url if url is None else url
            with tempfile.TemporaryDirectory() as temp_dir:
                download_path = Path(temp_dir).resolve()
                ytdl_options["outtmpl"] = str(download_path) + "/" + "test.%(ext)s"
                try:
                    with yt_dlp.YoutubeDL(ytdl_options) as ydl:
                        if extra_formats:
                            # yt-dlp takes no formats from the caller, so extract without
                            # processing, add them, then let it select and download.
                            info = ydl.extract_info(target_url, download=False, process=False)
                            info["formats"] = [*info.get("formats", ()), *extra_formats]
                            ydl.process_ie_result(info, download=True)
                        else:
                            ydl.download([target_url])
                # Unlike download(), process_ie_result() raises yt-dlp's specific
                # errors unwrapped; DownloadError shares their base class.
                except yt_dlp.utils.YoutubeDLError as e:
                    raise SiteDownloaderError(f"Youtube download failed: {e}") from e

                downloaded_files = list(download_path.iterdir())
                if downloaded_files:
                    downloaded_file = downloaded_files[0]
                else:
                    raise NotADownloadableLinkError(f"No media exists in the URL {target_url}")
                with downloaded_file.open("rb") as file:
                    content = file.read()
                return content

        return download

    @staticmethod
    def get_video_data(url: str) -> dict:
        yt_logger = logging.getLogger("youtube-dl")
        yt_logger.setLevel(logging.CRITICAL)
        with yt_dlp.YoutubeDL(
            {
                "logger": yt_logger,
            }
        ) as ydl:
            try:
                result = ydl.extract_info(url, download=False)
            except Exception as e:
                logger.exception(e)
                raise NotADownloadableLinkError(f"Video info extraction failed for {url}") from e
        return result

    @staticmethod
    def get_video_attributes(url: str) -> dict:
        result = Youtube.get_video_data(url)
        if "ext" in result:
            return result
        else:
            raise NotADownloadableLinkError(f"Video info extraction failed for {url}")
