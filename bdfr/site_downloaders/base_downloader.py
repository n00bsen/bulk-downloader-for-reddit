#!/usr/bin/env python3

import logging
from abc import ABC, abstractmethod

import requests
from praw.models import Submission

from bdfr import http_session
from bdfr.constants import REQUEST_TIMEOUT
from bdfr.exceptions import ResourceNotFound, SiteDownloaderError
from bdfr.resource import Resource
from bdfr.site_authenticator import SiteAuthenticator

logger = logging.getLogger(__name__)


class BaseDownloader(ABC):
    # Set by find_resources when it returns only part of the post's media, such
    # as a gallery image its host would not serve. The post is then not recorded
    # as downloaded, so the next run looks at it again.
    incomplete: bool = False

    def __init__(self, post: Submission, typical_extension: str | None = None):
        self.post = post
        self.typical_extension = typical_extension

    @abstractmethod
    def find_resources(self, authenticator: SiteAuthenticator | None = None) -> list[Resource]:
        """Return list of all un-downloaded Resources from submission"""
        raise NotImplementedError

    @staticmethod
    def retrieve_url(url: str, cookies: dict = None, headers: dict = None) -> requests.Response:
        res = BaseDownloader.fetch_url(url, cookies=cookies, headers=headers)
        BaseDownloader.require_ok(res, url)
        return res

    @staticmethod
    def fetch_url(url: str, cookies: dict = None, headers: dict = None) -> requests.Response:
        """GET a page through the thread's shared session, whatever its status.

        Separate from `retrieve_url` for callers that must react to a specific
        status before treating it as a failure.
        """
        try:
            return http_session.get_session().get(url, cookies=cookies, headers=headers, timeout=REQUEST_TIMEOUT)
        except requests.exceptions.RequestException as e:
            logger.exception(e)
            raise SiteDownloaderError(f"Failed to get page {url}") from e

    @staticmethod
    def require_ok(res: requests.Response, url: str) -> None:
        if res.status_code != 200:
            raise ResourceNotFound(f"Server responded with {res.status_code} to {url}")
