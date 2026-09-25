#!/usr/bin/env python3

import json
import logging
import re
import threading
from collections.abc import Callable

import requests
from praw.models import Submission

from bdfr import http_session
from bdfr.constants import REQUEST_TIMEOUT
from bdfr.exceptions import SiteDownloaderError
from bdfr.resource import Resource
from bdfr.site_authenticator import SiteAuthenticator
from bdfr.site_downloaders.base_downloader import BaseDownloader

logger = logging.getLogger(__name__)

AUTH_URL = "https://api.redgifs.com/v2/auth/temporary"

# Statuses with which the gifs API rejects an expired or revoked token.
TOKEN_REJECTED_STATUSES = (401, 403)

# Statuses with which a server may refuse a HEAD request that it would answer
# for GET, so a HEAD alone cannot settle whether the HD video is available.
HEAD_REJECTED_STATUSES = (403, 405, 501)


class _TokenCache:
    """This process's Redgifs API token, shared by every worker thread.

    A temporary token stays valid across many posts, so fetching a new one for
    each post doubled the API calls for no benefit.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._token: str | None = None

    def get(self, fetch: Callable[[], str], stale: str | None = None) -> str:
        """Return the cached token, fetching one if there is none or it is `stale`.

        A caller whose token was just rejected passes it as `stale`. If another
        thread has already replaced that token, the replacement is reused rather
        than every rejected worker fetching its own.
        """
        with self._lock:
            if self._token is None or self._token == stale:
                self._token = fetch()
            return self._token

    def clear(self) -> None:
        with self._lock:
            self._token = None


_token_cache = _TokenCache()


class Redgifs(BaseDownloader):
    def __init__(self, post: Submission):
        super().__init__(post)

    def find_resources(self, authenticator: SiteAuthenticator | None = None) -> list[Resource]:
        media_urls = self._get_link(self.post.url)
        return [Resource(self.post, m, Resource.retry_download(m), None) for m in media_urls]

    @staticmethod
    def _get_id(url: str) -> str:
        try:
            if url.endswith("/"):
                url = url.removesuffix("/")
            redgif_id = re.match(r".*/(.*?)(?:#.*|\?.*|\..{0,})?$", url).group(1).lower()
            if redgif_id.endswith("-mobile"):
                redgif_id = redgif_id.removesuffix("-mobile")
        except AttributeError:
            raise SiteDownloaderError(f"Could not extract Redgifs ID from {url}") from None
        return redgif_id

    @staticmethod
    def _fetch_auth_token() -> str:
        auth_token = json.loads(Redgifs.retrieve_url(AUTH_URL).text)["token"]
        if not auth_token:
            raise SiteDownloaderError("Unable to retrieve Redgifs API token")
        return auth_token

    @staticmethod
    def _api_headers(auth_token: str) -> dict[str, str]:
        return {
            "referer": "https://www.redgifs.com/",
            "origin": "https://www.redgifs.com",
            "content-type": "application/json",
            "Authorization": f"Bearer {auth_token}",
        }

    @staticmethod
    def _get_api_page(url: str) -> tuple[requests.Response, dict[str, str]]:
        """GET a Redgifs API URL with the cached token, returning the response and the headers that were accepted.

        The cached token can expire during a long run. A rejection is answered
        with one refresh and one retry; a second rejection is reported as before.
        """
        auth_token = _token_cache.get(Redgifs._fetch_auth_token)
        headers = Redgifs._api_headers(auth_token)
        content = Redgifs.fetch_url(url, headers=headers)
        if content.status_code in TOKEN_REJECTED_STATUSES:
            logger.debug(f"Redgifs rejected the cached API token with HTTP {content.status_code}, fetching a new one")
            auth_token = _token_cache.get(Redgifs._fetch_auth_token, stale=auth_token)
            headers = Redgifs._api_headers(auth_token)
            content = Redgifs.fetch_url(url, headers=headers)
        Redgifs.require_ok(content, url)
        return content, headers

    @staticmethod
    def _url_available(url: str, headers: dict[str, str]) -> bool:
        """Check that a video URL can be fetched without downloading the video.

        The download that follows fetches the video anyway, so reading the body
        here transferred every HD video twice.
        """
        session = http_session.get_session()
        response = session.head(url, headers=headers, allow_redirects=True, timeout=REQUEST_TIMEOUT)
        if response.status_code not in HEAD_REJECTED_STATUSES:
            return response.ok
        # Closing a streamed response unread discards the body after the headers.
        with session.get(url, headers=headers, stream=True, timeout=REQUEST_TIMEOUT) as response:
            return response.ok

    @staticmethod
    def _get_link(url: str) -> set[str]:
        redgif_id = Redgifs._get_id(url)

        content, headers = Redgifs._get_api_page(f"https://api.redgifs.com/v2/gifs/{redgif_id}")

        if content is None:
            raise SiteDownloaderError("Could not read the page source")

        try:
            response_json = json.loads(content.text)
        except json.JSONDecodeError as e:
            raise SiteDownloaderError(f"Received data was not valid JSON: {e}") from e

        out = set()
        try:
            if response_json["gif"]["type"] == 1:  # type 1 is a video
                if Redgifs._url_available(response_json["gif"]["urls"]["hd"], headers):
                    out.add(response_json["gif"]["urls"]["hd"])
                else:
                    out.add(response_json["gif"]["urls"]["sd"])
            elif response_json["gif"]["type"] == 2:  # type 2 is an image
                if response_json["gif"]["gallery"]:
                    # The gallery endpoint now answers 401 to requests without the token.
                    content, _ = Redgifs._get_api_page(
                        f"https://api.redgifs.com/v2/gallery/{response_json['gif']['gallery']}"
                    )
                    response_json = json.loads(content.text)
                    out = {p["urls"]["hd"] for p in response_json["gifs"]}
                else:
                    out.add(response_json["gif"]["urls"]["hd"])
            else:
                raise KeyError
        except (KeyError, AttributeError):
            raise SiteDownloaderError("Failed to find JSON data in page") from None

        # Update subdomain if old one is returned
        out = {re.sub("thumbs2", "thumbs3", link) for link in out}
        out = {re.sub("thumbs3", "thumbs4", link) for link in out}
        return out
