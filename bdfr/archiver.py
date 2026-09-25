#!/usr/bin/env python3

import functools
import json
import logging
import re
from collections.abc import Iterable
from pathlib import Path
from time import sleep

import dict2xml
import praw.models
import prawcore
import yaml

from bdfr.archive_entry.base_archive_entry import BaseArchiveEntry
from bdfr.archive_entry.comment_archive_entry import CommentArchiveEntry
from bdfr.archive_entry.submission_archive_entry import SubmissionArchiveEntry
from bdfr.concurrency import make_reddit_instance_thread_safe, process_submissions
from bdfr.configuration import Configuration
from bdfr.connector import RedditConnector
from bdfr.constants import DEFAULT_CONCURRENCY
from bdfr.exceptions import ArchiverError
from bdfr.listing import ResumableListing
from bdfr.resource import Resource

logger = logging.getLogger(__name__)


class Archiver(RedditConnector):
    def __init__(self, args: Configuration, logging_handlers: Iterable[logging.Handler] = ()):
        super().__init__(args, logging_handlers)

    @property
    def concurrency(self) -> int:
        """Number of entries archived at once; 1 reproduces the sequential path."""
        value = getattr(self.args, "concurrency", None) or DEFAULT_CONCURRENCY
        return max(1, int(value))

    def download(self):
        if self.concurrency == 1:
            self._archive_sequentially()
            return
        # Archiving is dominated by Reddit API calls, which are serialised for
        # safety, so concurrency here mainly overlaps formatting and disk writes.
        logger.info(f"Archiving with {self.concurrency} concurrent workers")
        make_reddit_instance_thread_safe(self.reddit_instance)
        stats = process_submissions(
            self.reddit_lists,
            handler=self._archive_submission,
            concurrency=self.concurrency,
            prepare=self._should_archive_submission,
            describe=lambda submission: str(getattr(submission, "id", "unknown")),
        )
        logger.info(
            f"Archive complete: {stats.completed} entries written, {stats.skipped} filtered out, {stats.failed} failed"
        )

    def _archive_sequentially(self):
        for generator in self.reddit_lists:
            submission = None
            try:
                for submission in generator:
                    try:
                        if not self._should_archive_submission(submission):
                            continue
                        self._archive_submission(submission)
                    except prawcore.PrawcoreException as e:
                        logger.error(f"Submission {submission.id} failed to be archived due to a PRAW exception: {e}")
            except prawcore.PrawcoreException as e:
                submission_id = submission.id if submission is not None else "unknown"
                logger.error(f"The submission after {submission_id} failed to download due to a PRAW exception: {e}")
                logger.debug("Waiting 60 seconds to continue")
                sleep(60)

    def _should_archive_submission(self, submission) -> bool:
        """Filter an item before it reaches a worker. Runs on the producer thread."""
        if (submission.author and submission.author.name in self.args.ignore_user) or (
            submission.author is None and "DELETED" in self.args.ignore_user
        ):
            ignored_name = submission.author.name if submission.author else "DELETED"
            logger.debug(f"Submission {submission.id} skipped due to {ignored_name} being an ignored user")
            return False
        if submission.id in self.excluded_submission_ids:
            logger.debug(f"Object {submission.id} in exclusion list, skipping")
            return False
        return True

    def _archive_submission(self, submission):
        logger.debug(f"Attempting to archive submission {submission.id}")
        self.write_entry(submission)

    def get_submissions_from_link(self) -> list[list[praw.models.Submission]]:
        supplied_submissions = []
        for sub_id in self.args.link:
            if len(sub_id) == 6:
                supplied_submissions.append(self.reddit_instance.submission(id=sub_id))
            elif re.match(r"^\w{7}$", sub_id):
                supplied_submissions.append(self.reddit_instance.comment(id=sub_id))
            else:
                supplied_submissions.append(self.reddit_instance.submission(url=sub_id))
        return [supplied_submissions]

    def get_user_data(self) -> list[ResumableListing]:
        results = super().get_user_data()
        if self.args.user and self.args.all_comments:
            sort = self.determine_sort_function()
            for user in self.args.user:
                logger.debug(f"Retrieving comments of user {user}")
                comments = functools.partial(sort, self.reddit_instance.redditor(user).comments)
                results.append(ResumableListing(comments, self.args.limit, f"comments of u/{user}", sleep=sleep))
        return results

    @staticmethod
    def _pull_lever_entry_factory(praw_item: praw.models.Submission | praw.models.Comment) -> BaseArchiveEntry:
        if isinstance(praw_item, praw.models.Submission):
            return SubmissionArchiveEntry(praw_item)
        elif isinstance(praw_item, praw.models.Comment):
            return CommentArchiveEntry(praw_item)
        else:
            raise ArchiverError(f"Factory failed to classify item of type {type(praw_item).__name__}")

    def write_entry(self, praw_item: praw.models.Submission | praw.models.Comment):
        if self.args.comment_context and isinstance(praw_item, praw.models.Comment):
            logger.debug(f"Converting comment {praw_item.id} to submission {praw_item.submission.id}")
            praw_item = praw_item.submission
        archive_entry = self._pull_lever_entry_factory(praw_item)
        if self.args.format == "json":
            self._write_entry_json(archive_entry)
        elif self.args.format == "xml":
            self._write_entry_xml(archive_entry)
        elif self.args.format == "yaml":
            self._write_entry_yaml(archive_entry)
        else:
            raise ArchiverError(f"Unknown format {self.args.format} given")
        logger.info(
            f"Record for entry item {praw_item.id} written to disk",
            extra={"bdfr_event": "submission_complete", "bdfr_submission_id": praw_item.id},
        )

    def _write_entry_json(self, entry: BaseArchiveEntry):
        resource = Resource(entry.source, "", lambda: None, ".json")
        content = json.dumps(entry.compile())
        self._write_content_to_disk(resource, content)

    def _write_entry_xml(self, entry: BaseArchiveEntry):
        resource = Resource(entry.source, "", lambda: None, ".xml")
        content = dict2xml.dict2xml(entry.compile(), wrap="root")
        self._write_content_to_disk(resource, content)

    def _write_entry_yaml(self, entry: BaseArchiveEntry):
        resource = Resource(entry.source, "", lambda: None, ".yaml")
        content = yaml.safe_dump(entry.compile())
        self._write_content_to_disk(resource, content)

    def _write_content_to_disk(self, resource: Resource, content: str):
        file_path = self.file_name_formatter.format_path(resource, self.download_directory)
        file_path.parent.mkdir(exist_ok=True, parents=True)
        with Path(file_path).open(mode="w", encoding="utf-8") as file:
            logger.debug(
                f"Writing entry {resource.source_submission.id} to file in {resource.extension[1:].upper()}"
                f" format at {file_path}"
            )
            file.write(content)
