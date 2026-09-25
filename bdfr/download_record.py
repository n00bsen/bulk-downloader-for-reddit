#!/usr/bin/env python3

"""Remember which submissions have already been downloaded in full.

Whether a post's files are on disk can normally only be decided after its site
downloader has run, because the file name depends on the extension the host
reports. For a video that means a yt-dlp extraction, for Redgifs an API call,
for a Reddit gallery a HEAD request per image, and all of it was repeated for
every post each time a user was downloaded again. A record of the posts that
were fully handled lets a re-run skip them before any of that network work.

A record only holds for the settings that produced it: a post saved under
{REDDITOR} is not on disk under {SUBREDDIT}, and a post whose mp4 was left out
by --skip has not been downloaded for a run that wants mp4 files. Every setting
that decides which files a post produces, and where, therefore goes into a
signature, and each signature gets its own record file. Workflows that share a
download folder cannot hide each other's posts.
"""

import hashlib
import json
import logging
import os
import re
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from bdfr.configuration import Configuration
from bdfr.exceptions import BulkDownloaderException
from bdfr.locking import atomic_write, file_lock

logger = logging.getLogger(__name__)

# BDFR's own state inside a download folder. Scans of the folder for existing
# media leave it out.
STATE_DIRECTORY_NAME = ".bdfr"
RECORDS_DIRECTORY = Path(STATE_DIRECTORY_NAME, "records")

# Part of the signature, so that a future change to what "downloaded" means
# starts fresh records instead of trusting ones written under the old meaning.
RECORD_FORMAT_VERSION = 1

# Hex digits of the settings hash used in the file name. 64 bits is plenty to
# tell apart the handful of workflows that write into one folder.
SIGNATURE_LENGTH = 16

# Reddit IDs are base 36 in lowercase. Any other line is damage or a hand edit.
_VALID_ID = re.compile(r"[a-z0-9]+")
_VALID_ID_BYTES = re.compile(rb"[a-z0-9]+")


def _sorted_entries(values: Iterable[Any] | str | None, lower: bool = False) -> list[str]:
    """Order-independent form of a list option, so "gif, avi" and "avi, gif" share a record."""
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    entries = {str(value).strip() for value in values}
    if lower:
        entries = {entry.lower() for entry in entries}
    return sorted(entry for entry in entries if entry)


def _optional_text(value: Any) -> str | None:
    text = "" if value is None else str(value).strip()
    return text or None


def record_settings(args: Configuration) -> dict[str, Any]:
    """Return every setting that decides which files a post produces and where they go.

    The values must be the effective ones, after the config file has filled in
    defaults, which is why the downloader calls this once it is set up.
    """
    return {
        "record_format": RECORD_FORMAT_VERSION,
        "folder_scheme": str(args.folder_scheme),
        "file_scheme": str(args.file_scheme),
        "filename_restriction_scheme": (_optional_text(args.filename_restriction_scheme) or "").lower() or None,
        # {DATE} in either scheme is rendered with it, so it moves files just as the schemes do.
        "time_format": _optional_text(args.time_format),
        "skip": _sorted_entries(args.skip),
        "skip_domain": _sorted_entries(args.skip_domain),
        "disable_module": _sorted_entries(args.disable_module, lower=True),
        "no_dupes": bool(args.no_dupes),
        "make_hard_links": bool(args.make_hard_links),
    }


def settings_signature(settings: dict[str, Any]) -> str:
    canonical = json.dumps(settings, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:SIGNATURE_LENGTH]


class DownloadRecord:
    """The IDs of submissions whose files are all on disk, for one set of settings.

    The file holds one ID per line and is only ever appended to, under a
    cross-process lock, so several GUI jobs writing into the same folder with
    the same settings share one record safely. An append interrupted by a crash
    leaves a line without its newline; that line is ignored on load and cut off
    before the next append, so a truncated ID can never match another post.
    """

    def __init__(self, path: Path, settings: dict[str, Any] | None = None):
        self.path = Path(path)
        self.settings = settings
        self._lock = threading.Lock()
        self._described = False
        self._ids = self._load(self.path)

    @classmethod
    def for_settings(cls, download_directory: Path, settings: dict[str, Any]) -> "DownloadRecord":
        signature = settings_signature(settings)
        return cls(Path(download_directory, RECORDS_DIRECTORY, f"{signature}.txt"), settings)

    @property
    def description_path(self) -> Path:
        return self.path.with_suffix(".json")

    def __contains__(self, submission_id: object) -> bool:
        with self._lock:
            return submission_id in self._ids

    def __len__(self) -> int:
        with self._lock:
            return len(self._ids)

    def add(self, submission_id: str) -> bool:
        """Record a submission as fully downloaded; return whether a new line was written.

        A failure to write is logged rather than raised: the files are already
        on disk, and the worst outcome is that the next run checks the post again.
        """
        if not isinstance(submission_id, str) or not _VALID_ID.fullmatch(submission_id):
            logger.warning(f"Not recording {submission_id!r} as downloaded: it is not a Reddit ID")
            return False
        with self._lock:
            if submission_id in self._ids:
                return False
            try:
                self._append(submission_id)
            except (OSError, BulkDownloaderException) as e:
                logger.warning(f"Could not record submission {submission_id} as downloaded in {self.path}: {e}")
                return False
            self._ids.add(submission_id)
        return True

    def _append(self, submission_id: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(self.path):
            if not self._described:
                self._write_description()
            if self._ends_mid_line():
                self._drop_unfinished_line()
            with self.path.open("ab") as handle:
                handle.write(f"{submission_id}\n".encode("ascii"))

    def _ends_mid_line(self) -> bool:
        try:
            with self.path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    return False
                handle.seek(-1, os.SEEK_END)
                return handle.read(1) != b"\n"
        except FileNotFoundError:
            return False

    def _drop_unfinished_line(self) -> None:
        # Only reached after a crash mid-append, so reading the whole file is fine.
        keep = self.path.read_bytes().rfind(b"\n") + 1
        logger.debug(f"Removing an unfinished line from the end of {self.path}")
        with self.path.open("r+b") as handle:
            handle.truncate(keep)

    def _write_description(self) -> None:
        """Say in a readable sibling file which settings the record belongs to.

        The record's name is a hash, so without this nobody could tell which
        file to delete to have one workflow's posts checked again.
        """
        if self.settings is not None and not self.description_path.exists():
            description = {
                "about": (
                    f"BDFR lists in {self.path.name} the posts it has fully downloaded into this folder with the "
                    "settings below, and skips them on later runs unless --recheck is given. Delete that file to "
                    "have every post checked again."
                ),
                "settings": self.settings,
            }
            atomic_write(self.description_path, json.dumps(description, indent=2) + "\n")
        self._described = True

    @staticmethod
    def _load(path: Path) -> set[str]:
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return set()
        except OSError as e:
            logger.warning(f"Could not read the download record {path}, every post will be checked: {e}")
            return set()
        # Whatever follows the last newline is an append that has not finished,
        # or never will; it is not trusted.
        *complete_lines, _unfinished = data.split(b"\n")
        return {
            line.decode("ascii") for line in (raw.strip() for raw in complete_lines) if _VALID_ID_BYTES.fullmatch(line)
        }
