#!/usr/bin/env python3

import hashlib
import logging.handlers
import os
import threading
import time
from collections.abc import Iterable
from datetime import datetime
from multiprocessing import Pool
from pathlib import Path
from time import sleep

import praw
import praw.exceptions
import praw.models
import prawcore

from bdfr import exceptions as errors
from bdfr.concurrency import make_reddit_instance_thread_safe, process_submissions
from bdfr.configuration import Configuration
from bdfr.connector import RedditConnector
from bdfr.constants import DEFAULT_CONCURRENCY
from bdfr.download_record import STATE_DIRECTORY_NAME, DownloadRecord, record_settings
from bdfr.locking import atomic_write_bytes
from bdfr.site_downloaders.download_factory import DownloadFactory

logger = logging.getLogger(__name__)

# Seconds a duplicate waits for the first copy of its content to be written.
# The content is already in memory, so this only has to outlast a slow disk.
DUPLICATE_WRITE_WAIT = 300


def _calc_hash(existing_file: Path):
    chunk_size = 1024 * 1024
    md5_hash = hashlib.md5()
    with existing_file.open("rb") as file:
        chunk = file.read(chunk_size)
        while chunk:
            md5_hash.update(chunk)
            chunk = file.read(chunk_size)
    file_hash = md5_hash.hexdigest()
    return existing_file, file_hash


class RedditDownloader(RedditConnector):
    # Whether posts recorded as fully downloaded are skipped, and new ones
    # recorded. RedditCloner turns this off because it also writes an archive
    # entry for every post, which a skip would silently leave out.
    uses_download_record = True

    def __init__(self, args: Configuration, logging_handlers: Iterable[logging.Handler] = ()):
        super().__init__(args, logging_handlers)
        if self.args.search_existing:
            self.master_hash_list = self.scan_existing_files(self.download_directory)
        # Guards master_hash_list, which several workers read and write.
        self._hash_lock = threading.Lock()
        # Set when the first write of each hash's content has finished, successfully or not.
        self._hash_writes: dict[str, threading.Event] = {}
        # Destinations currently being written, so two workers never race to
        # produce the same file.
        self._in_flight_destinations: set[Path] = set()
        self._in_flight_lock = threading.Lock()
        # Built from the effective settings, which the config file has filled in by now.
        self.download_record = self._open_download_record() if self.uses_download_record else None
        # Only the producer thread (or the sequential loop) touches this.
        self._already_downloaded_skips = 0

    def _open_download_record(self) -> DownloadRecord:
        record = DownloadRecord.for_settings(self.download_directory, record_settings(self.args))
        logger.debug(f"{len(record)} posts are recorded as already downloaded in {record.path}")
        return record

    @property
    def concurrency(self) -> int:
        """Number of resources fetched at once; 1 reproduces the sequential path."""
        value = getattr(self.args, "concurrency", None) or DEFAULT_CONCURRENCY
        return max(1, int(value))

    def download(self):
        if self.concurrency == 1:
            self._download_sequentially()
        else:
            logger.info(f"Downloading with {self.concurrency} concurrent workers")
            make_reddit_instance_thread_safe(self.reddit_instance)
            stats = process_submissions(
                self.reddit_lists,
                handler=self._download_submission_resources,
                concurrency=self.concurrency,
                prepare=self._should_download_submission,
                describe=lambda submission: str(getattr(submission, "id", "unknown")),
            )
            logger.info(
                f"Download complete: {stats.completed} submissions processed, "
                f"{stats.skipped - self._already_downloaded_skips} filtered out, {stats.failed} failed"
            )
        self._log_already_downloaded_summary()

    def _log_already_downloaded_summary(self) -> None:
        """Say why a re-run downloaded nothing; in the GUI this becomes the job's final message."""
        skipped = self._already_downloaded_skips
        if not skipped:
            return
        posts = "post" if skipped == 1 else "posts"
        logger.info(
            f"Skipped {skipped} {posts} already downloaded (use --recheck to verify them again)",
            extra={"bdfr_event": "run_summary"},
        )

    def _download_sequentially(self):
        for generator in self.reddit_lists:
            submission = None
            try:
                for submission in generator:
                    try:
                        self._download_submission(submission)
                    except prawcore.PrawcoreException as e:
                        logger.error(f"Submission {submission.id} failed to download due to a PRAW exception: {e}")
            except prawcore.PrawcoreException as e:
                submission_id = submission.id if submission is not None else "unknown"
                logger.error(f"The submission after {submission_id} failed to download due to a PRAW exception: {e}")
                logger.debug("Waiting 60 seconds to continue")
                sleep(60)

    def _download_submission(self, submission: praw.models.Submission):
        """Filter then download one submission on the calling thread."""
        if not self._should_download_submission(submission):
            return
        self._download_submission_resources(submission)

    def _should_download_submission(self, submission: praw.models.Submission) -> bool:
        """Decide whether a submission is worth downloading.

        This runs on the producer thread. Every Reddit attribute the filters
        touch is read here, which keeps the common case off the worker threads.
        The download record is consulted first because it needs nothing but the
        ID: a post already on disk costs no request of any kind.
        """
        if self.download_record is not None and not self.args.recheck and submission.id in self.download_record:
            logger.debug(f"Submission {submission.id} was already downloaded with these settings, skipping")
            self._already_downloaded_skips += 1
            return False
        if submission.id in self.excluded_submission_ids:
            logger.debug(f"Object {submission.id} in exclusion list, skipping")
            return False
        elif submission.subreddit.display_name.lower() in self.args.skip_subreddit:
            logger.debug(f"Submission {submission.id} in {submission.subreddit.display_name} in skip list")
            return False
        elif (submission.author and submission.author.name in self.args.ignore_user) or (
            submission.author is None and "DELETED" in self.args.ignore_user
        ):
            logger.debug(
                f"Submission {submission.id} in {submission.subreddit.display_name} skipped"
                f" due to {submission.author.name if submission.author else 'DELETED'} being an ignored user"
            )
            return False
        elif self.args.min_score and submission.score < self.args.min_score:
            logger.debug(
                f"Submission {submission.id} filtered due to score {submission.score} < [{self.args.min_score}]"
            )
            return False
        elif self.args.max_score and self.args.max_score < submission.score:
            logger.debug(
                f"Submission {submission.id} filtered due to score {submission.score} > [{self.args.max_score}]"
            )
            return False
        elif (self.args.min_score_ratio and submission.upvote_ratio < self.args.min_score_ratio) or (
            self.args.max_score_ratio and self.args.max_score_ratio < submission.upvote_ratio
        ):
            logger.debug(f"Submission {submission.id} filtered due to score ratio ({submission.upvote_ratio})")
            return False
        elif not isinstance(submission, praw.models.Submission):
            logger.warning(f"{submission.id} is not a submission")
            return False
        elif not self.download_filter.check_url(submission.url):
            logger.debug(f"Submission {submission.id} filtered due to URL {submission.url}")
            return False
        return True

    def _download_submission_resources(self, submission: praw.models.Submission):
        """Fetch and write every resource for an already-filtered submission.

        This runs on a worker thread. It performs non-Reddit HTTP, hashing and
        disk writes; shared state is guarded, and each file is written
        atomically so an interrupted run leaves no truncated files behind.

        The submission is recorded as downloaded only when every resource ended
        up on disk or was deliberately left out. Any failure leaves it
        unrecorded, so the next run tries it again.
        """
        logger.debug(f"Attempting to download submission {submission.id}")
        try:
            downloader_class = DownloadFactory.pull_lever(submission.url)
            downloader = downloader_class(submission)
            logger.debug(f"Using {downloader_class.__name__} with url {submission.url}")
        except errors.NotADownloadableLinkError as e:
            logger.error(f"Could not download submission {submission.id}: {e}")
            return
        if downloader_class.__name__.lower() in self.args.disable_module:
            logger.debug(f"Submission {submission.id} skipped due to disabled module {downloader_class.__name__}")
            return
        try:
            content = downloader.find_resources(self.authenticator)
        except errors.SiteDownloaderError as e:
            logger.error(f"Site {downloader_class.__name__} failed to download submission {submission.id}: {e}")
            return
        resource_paths = self.file_name_formatter.format_resource_paths(content, self.download_directory)
        # The formatter drops, with an error, a resource it cannot name; a site
        # downloader flags media it knows it could not find; and a post with
        # nothing to download may only have hit a passing fault. None is done.
        complete = bool(content) and len(resource_paths) == len(content) and not downloader.incomplete
        for destination, res in resource_paths:
            if not self._download_resource(submission, destination, res, downloader_class.__name__):
                complete = False
        logger.info(
            f"Downloaded submission {submission.id} from {submission.subreddit.display_name}",
            extra={"bdfr_event": "submission_complete", "bdfr_submission_id": submission.id},
        )
        if complete and self.download_record is not None:
            self.download_record.add(submission.id)

    def _download_resource(
        self, submission: praw.models.Submission, destination: Path, res, downloader_name: str
    ) -> bool:
        """Bring one resource to disk; return whether nothing more is needed for it."""
        if destination.exists():
            logger.debug(f"File {destination} from submission {submission.id} already exists, continuing")
            return True
        if not self.download_filter.check_resource(res):
            logger.debug(f"Download filter removed {submission.id} file with URL {submission.url}")
            return True
        if not self._claim_destination(destination):
            # The other worker may yet fail, so this one cannot count the file as done.
            logger.debug(f"File {destination} is already being written by another worker, skipping")
            return False
        try:
            return self._write_resource(submission, destination, res, downloader_name)
        finally:
            self._release_destination(destination)

    def _claim_destination(self, destination: Path) -> bool:
        """Reserve a destination path for this worker.

        Two submissions can format to the same path; without a claim both
        workers would download and write the same file.
        """
        with self._in_flight_lock:
            if destination in self._in_flight_destinations:
                return False
            self._in_flight_destinations.add(destination)
            return True

    def _release_destination(self, destination: Path) -> None:
        with self._in_flight_lock:
            self._in_flight_destinations.discard(destination)

    def _write_resource(
        self,
        submission: praw.models.Submission,
        destination: Path,
        res,
        downloader_name: str,
    ) -> bool:
        """Download one resource and write, link or deliberately skip it; return whether that succeeded."""
        try:
            res.download({"max_wait_time": self.args.max_wait_time})
        except errors.BulkDownloaderException as e:
            logger.error(
                f"Failed to download resource {res.url} in submission {submission.id} "
                f"with downloader {downloader_name}: {e}"
            )
            return False
        resource_hash = res.hash.hexdigest()
        destination.parent.mkdir(parents=True, exist_ok=True)

        # Read and update the shared hash list under one lock so that two
        # workers finishing identical content cannot both decide they are first.
        pending_write = None
        with self._hash_lock:
            is_duplicate = resource_hash in self.master_hash_list and (self.args.no_dupes or self.args.make_hard_links)
            if is_duplicate:
                existing = self.master_hash_list[resource_hash]
                pending_write = self._hash_writes.get(resource_hash)
            else:
                self.master_hash_list[resource_hash] = destination
                write_finished = self._hash_writes[resource_hash] = threading.Event()
                logger.debug(f"Hash added to master list: {resource_hash}")
        if is_duplicate:
            return self._handle_duplicate(submission, destination, resource_hash, existing, pending_write)

        try:
            atomic_write_bytes(destination, res.content)
            logger.debug(f"Written file to {destination}")
        except OSError as e:
            logger.exception(e)
            logger.error(f"Failed to write file in submission {submission.id} to {destination}: {e}")
            # The hash was reserved before writing; drop it so a later attempt
            # is not mistaken for a duplicate of a file that does not exist.
            with self._hash_lock:
                if self.master_hash_list.get(resource_hash) == destination:
                    del self.master_hash_list[resource_hash]
            return False
        finally:
            # Duplicates of this content wait on it before counting as done.
            write_finished.set()
        creation_time = time.mktime(datetime.fromtimestamp(submission.created_utc).timetuple())
        os.utime(destination, (creation_time, creation_time))
        return True

    def _handle_duplicate(
        self,
        submission: praw.models.Submission,
        destination: Path,
        resource_hash: str,
        existing: Path | None,
        pending_write: threading.Event | None,
    ) -> bool:
        """Skip or hard-link a resource whose content another resource already has.

        The hash is reserved before its file is written, so the first copy may
        still be in flight or may yet fail. A duplicate only counts as done once
        that copy exists on disk; otherwise the post would be recorded as fully
        downloaded while its content exists nowhere.
        """
        if pending_write is not None and not pending_write.wait(timeout=DUPLICATE_WRITE_WAIT):
            logger.warning(
                f"Timed out waiting for {existing} to be written; submission {submission.id} will be checked again"
            )
            return False
        # None is an unknown location, as a hash list from outside this run may hold.
        if existing is not None and not existing.exists():
            logger.warning(
                f"Resource hash {resource_hash} from submission {submission.id} matches {existing}, "
                "which was not written; submission will be checked again"
            )
            return False
        if self.args.no_dupes:
            logger.info(f"Resource hash {resource_hash} from submission {submission.id} downloaded elsewhere")
            return True
        try:
            destination.hardlink_to(existing)
        except (OSError, TypeError) as e:
            logger.error(f"Failed to hard link {destination} to {existing}: {e}")
            return False
        logger.info(f"Hard link made linking {destination} to {existing} in submission {submission.id}")
        return True

    @staticmethod
    def scan_existing_files(directory: Path) -> dict[str, Path]:
        files = []
        for dirpath, dirnames, filenames in os.walk(directory):
            # BDFR's own state is not media, and a record file may be mid-append.
            dirnames[:] = [name for name in dirnames if name != STATE_DIRECTORY_NAME]
            files.extend([Path(dirpath, file) for file in filenames])
        logger.info(f"Calculating hashes for {len(files)} files")

        pool = Pool(15)
        results = pool.map(_calc_hash, files)
        pool.close()

        hash_list = {res[1]: res[0] for res in results}
        return hash_list
