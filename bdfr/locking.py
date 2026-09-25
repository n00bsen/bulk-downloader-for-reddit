#!/usr/bin/env python3

"""Cross-process coordination primitives.

BDFR historically assumed a single running instance: it rewrote the shared
configuration file on every startup and rolled over one shared log file. Running
several downloads at once made those writes race, which could truncate the
config file and lose a stored OAuth refresh token. The helpers here let the
config and token paths serialise their writes across processes.
"""

import logging
import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from bdfr.exceptions import BulkDownloaderException

logger = logging.getLogger(__name__)

LOCK_SUFFIX = ".lock"
DEFAULT_LOCK_TIMEOUT = 30.0


class LockTimeoutError(BulkDownloaderException):
    """Raised when a lock could not be acquired within the allotted time."""


def _try_lock_handle(handle) -> bool:
    """Try to take an exclusive OS-level lock on an open file handle.

    Returns True if the lock was taken, False if another process holds it. An
    OS-level lock is used rather than a sentinel file so that the lock is
    released automatically if the holding process dies, which avoids a crashed
    download wedging every later run.
    """
    if sys.platform == "win32":
        import msvcrt

        # msvcrt locks a byte range starting at the handle's current position,
        # so every participant must agree on the same offset.
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock_handle(handle) -> None:
    if sys.platform == "win32":
        import msvcrt

        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            logger.log(9, "Lock was already released")
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def file_lock(target: Path, timeout: float = DEFAULT_LOCK_TIMEOUT) -> Iterator[None]:
    """Hold an exclusive lock covering `target` for the duration of the block.

    The lock lives in a sidecar `<target>.lock` file so that the target itself
    can be freely replaced while the lock is held, which is what
    `atomic_write` relies on.
    """
    lock_path = Path(f"{target}{LOCK_SUFFIX}")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    with lock_path.open("a+b") as handle:
        while True:
            if _try_lock_handle(handle):
                break
            if time.monotonic() >= deadline:
                raise LockTimeoutError(f"Could not acquire lock on {target} within {timeout} seconds")
            time.sleep(0.05)
        try:
            yield
        finally:
            _unlock_handle(handle)


def atomic_write(target: Path, content: str, encoding: str = "utf-8") -> None:
    """Replace `target` with `content` in a single filesystem operation.

    The content is staged in a sibling temporary file and moved into place with
    `os.replace`, so a reader never observes a half-written file and an
    interrupted write leaves the previous version intact.
    """
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    try:
        with temp_path.open("w", encoding=encoding, newline="") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_path, target)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def atomic_write_bytes(target: Path, content: bytes) -> None:
    """Byte-oriented counterpart to `atomic_write`, used for downloaded media."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    try:
        with temp_path.open("wb") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_path, target)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
