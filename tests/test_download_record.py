#!/usr/bin/env python3

"""Tests for the record of fully downloaded submissions.

The multi-process target below must stay a module-level function: it is started
with the "spawn" method, as the GUI's jobs are, so the child has to import it
by qualified name.
"""

import json
import multiprocessing
import threading
from pathlib import Path

import pytest

from bdfr.configuration import Configuration
from bdfr.download_record import (
    RECORDS_DIRECTORY,
    SIGNATURE_LENGTH,
    DownloadRecord,
    record_settings,
    settings_signature,
)


def _append_ids(record_path: str, ids: list[str]) -> None:
    record = DownloadRecord(Path(record_path), {"test": True})
    for submission_id in ids:
        assert record.add(submission_id)


def _args(**overrides) -> Configuration:
    args = Configuration()
    args.time_format = "ISO"
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def test_missing_record_is_empty(tmp_path: Path):
    record = DownloadRecord(tmp_path / "absent.txt")
    assert len(record) == 0
    assert "abc123" not in record


def test_add_then_contains_and_survives_reload(tmp_path: Path):
    path = tmp_path / "record.txt"
    record = DownloadRecord(path)
    assert record.add("abc123") is True
    assert "abc123" in record
    assert "zzz999" not in record
    assert "abc123" in DownloadRecord(path)


def test_add_twice_writes_one_line(tmp_path: Path):
    path = tmp_path / "record.txt"
    record = DownloadRecord(path)
    assert record.add("abc123") is True
    assert record.add("abc123") is False
    assert path.read_text(encoding="ascii") == "abc123\n"


@pytest.mark.parametrize("bad_id", ("", "ABC123", "abc/123", "abc\n123", "abc 123", None, 12345))
def test_add_refuses_what_is_not_a_reddit_id(bad_id, tmp_path: Path):
    path = tmp_path / "record.txt"
    record = DownloadRecord(path)
    assert record.add(bad_id) is False
    assert not path.exists()


def test_load_ignores_blank_malformed_and_unfinished_lines(tmp_path: Path):
    path = tmp_path / "record.txt"
    path.write_bytes(b"abc123\n\n   \nNOT VALID\n  def456  \r\nghi\x00789\nxyz789")
    record = DownloadRecord(path)
    # "xyz789" has no newline: an append that never finished, so it is not trusted.
    assert len(record) == 2
    assert "abc123" in record
    assert "def456" in record
    assert "xyz789" not in record


def test_add_after_a_torn_append_drops_the_fragment(tmp_path: Path):
    """A fragment such as "abc" of "abcdef" must never become a line of its own."""
    path = tmp_path / "record.txt"
    path.write_bytes(b"first1\nabc")
    record = DownloadRecord(path)
    assert record.add("second") is True
    assert path.read_bytes() == b"first1\nsecond\n"
    assert set(DownloadRecord(path)._ids) == {"first1", "second"}


def test_unreadable_record_is_treated_as_empty_and_add_does_not_raise(tmp_path: Path):
    path = tmp_path / "record.txt"
    path.mkdir()
    record = DownloadRecord(path)
    assert len(record) == 0
    assert record.add("abc123") is False
    assert "abc123" not in record


def test_record_lives_under_the_download_directory(tmp_path: Path):
    settings = record_settings(_args())
    record = DownloadRecord.for_settings(tmp_path, settings)
    signature = settings_signature(settings)
    assert len(signature) == SIGNATURE_LENGTH
    assert record.path == tmp_path / RECORDS_DIRECTORY / f"{signature}.txt"
    assert record.description_path == tmp_path / RECORDS_DIRECTORY / f"{signature}.json"


def test_description_names_the_settings(tmp_path: Path):
    settings = record_settings(_args(folder_scheme="{REDDITOR}", skip=["gif", "avi"]))
    record = DownloadRecord.for_settings(tmp_path, settings)
    assert not record.description_path.exists(), "nothing is written until a post is recorded"
    record.add("abc123")
    description = json.loads(record.description_path.read_text(encoding="utf-8"))
    assert description["settings"] == settings
    assert description["settings"]["folder_scheme"] == "{REDDITOR}"
    assert description["settings"]["skip"] == ["avi", "gif"]
    assert record.path.name in description["about"]


def test_signature_is_stable_for_equal_settings():
    first = record_settings(_args(skip=["gif", "avi"], skip_domain=["b.com", "a.com"], disable_module={"Youtube"}))
    second = record_settings(_args(skip=("avi", "gif"), skip_domain=["a.com", "b.com"], disable_module=["youtube"]))
    assert settings_signature(first) == settings_signature(second)


@pytest.mark.parametrize(
    ("setting", "value"),
    (
        ("folder_scheme", "{SUBREDDIT}/{REDDITOR}"),
        ("file_scheme", "{POSTID}"),
        ("filename_restriction_scheme", "windows"),
        ("time_format", "%Y-%m-%d"),
        ("skip", ["gif", "avi", "mp4"]),
        ("skip_domain", ["redgifs.com"]),
        ("disable_module", ["youtube"]),
        ("no_dupes", True),
        ("make_hard_links", True),
    ),
)
def test_signature_changes_with_every_setting_that_moves_files(setting: str, value):
    baseline = _args(skip=["gif", "avi"])
    changed = _args(**({"skip": ["gif", "avi"]} | {setting: value}))
    assert settings_signature(record_settings(baseline)) != settings_signature(record_settings(changed))


def test_settings_that_do_not_decide_files_share_a_record():
    baseline = record_settings(_args())
    other = record_settings(_args(sort="top", limit=5, concurrency=9, user=["alice"], recheck=True))
    assert settings_signature(baseline) == settings_signature(other)


def test_workflows_in_one_folder_keep_separate_records(tmp_path: Path):
    """Users saved by {REDDITOR} must not hide subreddit posts saved by {SUBREDDIT}, and back."""
    users = DownloadRecord.for_settings(tmp_path, record_settings(_args(folder_scheme="{REDDITOR}")))
    videos = DownloadRecord.for_settings(
        tmp_path, record_settings(_args(folder_scheme="{SUBREDDIT}", skip=["jpg", "png"]))
    )
    assert users.path != videos.path
    users.add("abc123")
    videos.add("def456")

    users_again = DownloadRecord.for_settings(tmp_path, record_settings(_args(folder_scheme="{REDDITOR}")))
    assert "abc123" in users_again
    assert "def456" not in users_again
    videos_again = DownloadRecord.for_settings(
        tmp_path, record_settings(_args(folder_scheme="{SUBREDDIT}", skip=["png", "jpg"]))
    )
    assert "def456" in videos_again
    assert "abc123" not in videos_again


def test_threads_adding_at_once_lose_nothing(tmp_path: Path):
    path = tmp_path / "record.txt"
    record = DownloadRecord(path)
    batches = [[f"t{thread}n{index}" for index in range(50)] for thread in range(8)]
    threads = [threading.Thread(target=lambda ids=ids: [record.add(i) for i in ids]) for ids in batches]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    expected = {submission_id for batch in batches for submission_id in batch}
    lines = path.read_text(encoding="ascii").splitlines()
    assert sorted(lines) == sorted(expected)


@pytest.mark.slow
def test_processes_appending_at_once_interleave_whole_lines(tmp_path: Path):
    """GUI jobs are separate processes and may append to the same record."""
    path = tmp_path / RECORDS_DIRECTORY / "shared.txt"
    context = multiprocessing.get_context("spawn")
    batches = [[f"p{worker}n{index}" for index in range(60)] for worker in range(4)]
    processes = [context.Process(target=_append_ids, args=(str(path), ids)) for ids in batches]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=120)
    assert [process.exitcode for process in processes] == [0, 0, 0, 0]

    expected = {submission_id for batch in batches for submission_id in batch}
    raw = path.read_bytes()
    assert raw.endswith(b"\n")
    lines = raw.decode("ascii").split("\n")[:-1]
    assert len(lines) == len(expected), "a line was lost or torn"
    assert set(lines) == expected
    assert set(DownloadRecord(path)._ids) == expected
