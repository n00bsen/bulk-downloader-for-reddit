#!/usr/bin/env python3

import logging
from collections.abc import Iterable
from time import sleep

import prawcore

from bdfr.archiver import Archiver
from bdfr.concurrency import make_reddit_instance_thread_safe, process_submissions
from bdfr.configuration import Configuration
from bdfr.downloader import RedditDownloader

logger = logging.getLogger(__name__)


class RedditCloner(RedditDownloader, Archiver):
    # The record says a post's media is on disk, not that its archive entry is,
    # so skipping recorded posts here would leave their entries unwritten.
    uses_download_record = False

    def __init__(self, args: Configuration, logging_handlers: Iterable[logging.Handler] = ()):
        super().__init__(args, logging_handlers)

    def download(self):
        if self.concurrency == 1:
            self._clone_sequentially()
            return
        logger.info(f"Cloning with {self.concurrency} concurrent workers")
        make_reddit_instance_thread_safe(self.reddit_instance)
        stats = process_submissions(
            self.reddit_lists,
            handler=self._clone_submission,
            concurrency=self.concurrency,
            prepare=self._should_download_submission,
            describe=lambda submission: str(getattr(submission, "id", "unknown")),
        )
        logger.info(
            f"Clone complete: {stats.completed} submissions processed, "
            f"{stats.skipped} filtered out, {stats.failed} failed"
        )

    def _clone_sequentially(self):
        for generator in self.reddit_lists:
            submission = None
            try:
                for submission in generator:
                    try:
                        self._download_submission(submission)
                        self.write_entry(submission)
                    except prawcore.PrawcoreException as e:
                        logger.error(f"Submission {submission.id} failed to be cloned due to a PRAW exception: {e}")
            except prawcore.PrawcoreException as e:
                submission_id = submission.id if submission is not None else "unknown"
                logger.error(f"The submission after {submission_id} failed to download due to a PRAW exception: {e}")
                logger.debug("Waiting 60 seconds to continue")
                sleep(60)

    def _clone_submission(self, submission):
        """Download the media and write the archive entry for one submission."""
        self._download_submission_resources(submission)
        self.write_entry(submission)
