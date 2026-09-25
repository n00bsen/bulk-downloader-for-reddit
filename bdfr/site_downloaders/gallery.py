#!/usr/bin/env python3

import logging

from praw.models import Submission

from bdfr import http_session
from bdfr.constants import REQUEST_TIMEOUT
from bdfr.exceptions import SiteDownloaderError
from bdfr.resource import Resource
from bdfr.site_authenticator import SiteAuthenticator
from bdfr.site_downloaders.base_downloader import BaseDownloader

logger = logging.getLogger(__name__)


class Gallery(BaseDownloader):
    def __init__(self, post: Submission):
        super().__init__(post)

    def find_resources(self, authenticator: SiteAuthenticator | None = None) -> list[Resource]:
        try:
            items = self.post.gallery_data["items"]
            image_urls = self._get_links(items)
        except (AttributeError, TypeError):
            try:
                items = self.post.crosspost_parent_list[0]["gallery_data"]["items"]
                image_urls = self._get_links(items)
            except (AttributeError, IndexError, TypeError, KeyError) as e:
                logger.error(f"Could not find gallery data in submission {self.post.id}")
                logger.exception("Gallery image find failure")
                raise SiteDownloaderError("No images found in Reddit gallery") from e

        if not image_urls:
            raise SiteDownloaderError("No images found in Reddit gallery")
        if len(image_urls) < len(items):
            # A refused probe may be a passing fault, so the post must be looked at again.
            logger.warning(
                f"Found {len(image_urls)} of {len(items)} images in the gallery of submission {self.post.id}"
            )
            self.incomplete = True
        return [Resource(self.post, url, Resource.retry_download(url)) for url in image_urls]

    @staticmethod
    def _get_links(id_dict: list[dict]) -> list[str]:
        out = []
        for item in id_dict:
            image_id = item["media_id"]
            possible_extensions = (".jpg", ".png", ".gif", ".gifv", ".jpeg")
            for extension in possible_extensions:
                test_url = f"https://i.redd.it/{image_id}{extension}"
                response = http_session.get_session().head(test_url, timeout=REQUEST_TIMEOUT)
                if response.status_code == 200:
                    out.append(test_url)
                    break
        return out
