#!/usr/bin/env python3
import logging
import re
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import praw.models
import pytest

from bdfr import exceptions as errors
from bdfr.__main__ import cli_download, make_console_logging_handler
from bdfr.cloner import RedditCloner
from bdfr.configuration import Configuration
from bdfr.connector import RedditConnector
from bdfr.download_filter import DownloadFilter
from bdfr.download_record import STATE_DIRECTORY_NAME, DownloadRecord, record_settings
from bdfr.downloader import RedditDownloader


def add_console_handler():
    logging.getLogger().addHandler(make_console_logging_handler(3))


@pytest.fixture()
def args() -> Configuration:
    args = Configuration()
    args.time_format = "ISO"
    return args


@pytest.fixture()
def downloader_mock(args: Configuration):
    downloader_mock = MagicMock()
    downloader_mock.args = args
    downloader_mock._sanitise_subreddit_name = RedditConnector.sanitise_subreddit_name
    downloader_mock._split_args_input = RedditConnector.split_args_input
    downloader_mock.master_hash_list = {}
    # Real shared-state guards, so the download path behaves as it does in production.
    downloader_mock._hash_lock = threading.Lock()
    downloader_mock._hash_writes = {}
    downloader_mock._in_flight_lock = threading.Lock()
    downloader_mock._in_flight_destinations = set()
    # No record unless a test asks for one, so nothing is skipped or recorded.
    downloader_mock.download_record = None
    downloader_mock._already_downloaded_skips = 0
    # Bind the real download stages; _download_submission only orchestrates them.
    downloader_mock._should_download_submission = lambda submission: RedditDownloader._should_download_submission(
        downloader_mock, submission
    )
    downloader_mock._download_submission_resources = lambda submission: RedditDownloader._download_submission_resources(
        downloader_mock, submission
    )
    downloader_mock._download_resource = lambda *call_args: RedditDownloader._download_resource(
        downloader_mock, *call_args
    )
    downloader_mock._log_already_downloaded_summary = lambda: RedditDownloader._log_already_downloaded_summary(
        downloader_mock
    )
    downloader_mock._claim_destination = lambda destination: RedditDownloader._claim_destination(
        downloader_mock, destination
    )
    downloader_mock._release_destination = lambda destination: RedditDownloader._release_destination(
        downloader_mock, destination
    )
    downloader_mock._write_resource = lambda *call_args: RedditDownloader._write_resource(downloader_mock, *call_args)
    downloader_mock._handle_duplicate = lambda *call_args: RedditDownloader._handle_duplicate(
        downloader_mock, *call_args
    )
    return downloader_mock


@pytest.mark.parametrize(
    ("test_ids", "test_excluded", "expected_len"),
    (
        (("aaaaaa",), (), 1),
        (("aaaaaa",), ("aaaaaa",), 0),
        ((), ("aaaaaa",), 0),
        (("aaaaaa", "bbbbbb"), ("aaaaaa",), 1),
        (("aaaaaa", "bbbbbb", "cccccc"), ("aaaaaa",), 2),
    ),
)
@patch("bdfr.site_downloaders.download_factory.DownloadFactory.pull_lever")
def test_excluded_ids(
    mock_function: MagicMock,
    test_ids: tuple[str],
    test_excluded: tuple[str],
    expected_len: int,
    downloader_mock: MagicMock,
):
    downloader_mock.excluded_submission_ids = test_excluded
    mock_function.return_value = MagicMock()
    mock_function.return_value.__name__ = "test"
    test_submissions = []
    for test_id in test_ids:
        m = MagicMock()
        m.id = test_id
        m.subreddit.display_name.return_value = "https://www.example.com/"
        m.__class__ = praw.models.Submission
        test_submissions.append(m)
    downloader_mock.reddit_lists = [test_submissions]
    for submission in test_submissions:
        RedditDownloader._download_submission(downloader_mock, submission)
    assert mock_function.call_count == expected_len


@pytest.mark.online
@pytest.mark.reddit
@pytest.mark.parametrize("test_submission_id", ("m1hqw6",))
def test_mark_hard_link(
    test_submission_id: str, downloader_mock: MagicMock, tmp_path: Path, reddit_instance: praw.Reddit
):
    downloader_mock.reddit_instance = reddit_instance
    downloader_mock.args.make_hard_links = True
    downloader_mock.download_directory = tmp_path
    downloader_mock.args.folder_scheme = ""
    downloader_mock.args.file_scheme = "{POSTID}"
    downloader_mock.file_name_formatter = RedditConnector.create_file_name_formatter(downloader_mock)
    submission = downloader_mock.reddit_instance.submission(id=test_submission_id)
    original = Path(tmp_path, f"{test_submission_id}.png")

    RedditDownloader._download_submission(downloader_mock, submission)
    assert original.exists()

    downloader_mock.args.file_scheme = "test2_{POSTID}"
    downloader_mock.file_name_formatter = RedditConnector.create_file_name_formatter(downloader_mock)
    RedditDownloader._download_submission(downloader_mock, submission)
    test_file_1_stats = original.stat()
    test_file_2_inode = Path(tmp_path, f"test2_{test_submission_id}.png").stat().st_ino

    assert test_file_1_stats.st_nlink == 2
    assert test_file_1_stats.st_ino == test_file_2_inode


@pytest.mark.online
@pytest.mark.reddit
@pytest.mark.parametrize(("test_submission_id", "test_creation_date"), (("ndzz50", 1621204841.0),))
def test_file_creation_date(
    test_submission_id: str,
    test_creation_date: float,
    downloader_mock: MagicMock,
    tmp_path: Path,
    reddit_instance: praw.Reddit,
):
    downloader_mock.reddit_instance = reddit_instance
    downloader_mock.download_directory = tmp_path
    downloader_mock.args.folder_scheme = ""
    downloader_mock.args.file_scheme = "{POSTID}"
    downloader_mock.file_name_formatter = RedditConnector.create_file_name_formatter(downloader_mock)
    submission = downloader_mock.reddit_instance.submission(id=test_submission_id)

    RedditDownloader._download_submission(downloader_mock, submission)

    for file_path in Path(tmp_path).iterdir():
        file_stats = Path(file_path).stat()
        assert file_stats.st_mtime == test_creation_date


def test_search_existing_files():
    results = RedditDownloader.scan_existing_files(Path())
    assert len(results.keys()) != 0


@pytest.mark.online
@pytest.mark.reddit
@pytest.mark.parametrize(("test_submission_id", "test_hash"), (("m1hqw6", "a912af8905ae468e0121e9940f797ad7"),))
def test_download_submission_hash_exists(
    test_submission_id: str,
    test_hash: str,
    downloader_mock: MagicMock,
    reddit_instance: praw.Reddit,
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
):
    add_console_handler()
    downloader_mock.reddit_instance = reddit_instance
    downloader_mock.download_filter.check_url.return_value = True
    downloader_mock.args.folder_scheme = ""
    downloader_mock.args.no_dupes = True
    downloader_mock.file_name_formatter = RedditConnector.create_file_name_formatter(downloader_mock)
    downloader_mock.download_directory = tmp_path
    downloader_mock.master_hash_list = {test_hash: None}
    submission = downloader_mock.reddit_instance.submission(id=test_submission_id)
    RedditDownloader._download_submission(downloader_mock, submission)
    folder_contents = list(tmp_path.iterdir())
    output = capsys.readouterr()
    assert not folder_contents
    assert re.search(r"Resource hash .*? downloaded elsewhere", output.out)


@pytest.mark.online
@pytest.mark.reddit
def test_download_submission_file_exists(
    downloader_mock: MagicMock, reddit_instance: praw.Reddit, tmp_path: Path, capsys: pytest.CaptureFixture
):
    add_console_handler()
    downloader_mock.reddit_instance = reddit_instance
    downloader_mock.download_filter.check_url.return_value = True
    downloader_mock.args.folder_scheme = ""
    downloader_mock.file_name_formatter = RedditConnector.create_file_name_formatter(downloader_mock)
    downloader_mock.download_directory = tmp_path
    submission = downloader_mock.reddit_instance.submission(id="m1hqw6")
    Path(tmp_path, "Arneeman_Metagaming isn't always a bad thing_m1hqw6.png").touch()
    RedditDownloader._download_submission(downloader_mock, submission)
    folder_contents = list(tmp_path.iterdir())
    output = capsys.readouterr()
    assert len(folder_contents) == 1
    assert "Arneeman_Metagaming isn't always a bad thing_m1hqw6.png from submission m1hqw6 already exists" in output.out


@pytest.mark.online
@pytest.mark.reddit
@pytest.mark.parametrize(("test_submission_id", "expected_files_len"), (("ljyy27", 4),))
def test_download_submission(
    test_submission_id: str,
    expected_files_len: int,
    downloader_mock: MagicMock,
    reddit_instance: praw.Reddit,
    tmp_path: Path,
):
    downloader_mock.reddit_instance = reddit_instance
    downloader_mock.download_filter.check_url.return_value = True
    downloader_mock.args.folder_scheme = ""
    downloader_mock.file_name_formatter = RedditConnector.create_file_name_formatter(downloader_mock)
    downloader_mock.download_directory = tmp_path
    submission = downloader_mock.reddit_instance.submission(id=test_submission_id)
    RedditDownloader._download_submission(downloader_mock, submission)
    folder_contents = list(tmp_path.iterdir())
    assert len(folder_contents) == expected_files_len


@pytest.mark.online
@pytest.mark.reddit
@pytest.mark.parametrize(("test_submission_id", "min_score"), (("ljyy27", 1),))
def test_download_submission_min_score_above(
    test_submission_id: str,
    min_score: int,
    downloader_mock: MagicMock,
    reddit_instance: praw.Reddit,
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
):
    add_console_handler()
    downloader_mock.reddit_instance = reddit_instance
    downloader_mock.download_filter.check_url.return_value = True
    downloader_mock.args.folder_scheme = ""
    downloader_mock.args.min_score = min_score
    downloader_mock.file_name_formatter = RedditConnector.create_file_name_formatter(downloader_mock)
    downloader_mock.download_directory = tmp_path
    submission = downloader_mock.reddit_instance.submission(id=test_submission_id)
    RedditDownloader._download_submission(downloader_mock, submission)
    output = capsys.readouterr()
    assert "filtered due to score" not in output.out


@pytest.mark.online
@pytest.mark.reddit
@pytest.mark.parametrize(("test_submission_id", "min_score"), (("ljyy27", 25),))
def test_download_submission_min_score_below(
    test_submission_id: str,
    min_score: int,
    downloader_mock: MagicMock,
    reddit_instance: praw.Reddit,
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
):
    add_console_handler()
    downloader_mock.reddit_instance = reddit_instance
    downloader_mock.download_filter.check_url.return_value = True
    downloader_mock.args.folder_scheme = ""
    downloader_mock.args.min_score = min_score
    downloader_mock.file_name_formatter = RedditConnector.create_file_name_formatter(downloader_mock)
    downloader_mock.download_directory = tmp_path
    submission = downloader_mock.reddit_instance.submission(id=test_submission_id)
    RedditDownloader._download_submission(downloader_mock, submission)
    output = capsys.readouterr()
    assert "filtered due to score" in output.out


@pytest.mark.online
@pytest.mark.reddit
@pytest.mark.parametrize(("test_submission_id", "max_score"), (("ljyy27", 25),))
def test_download_submission_max_score_below(
    test_submission_id: str,
    max_score: int,
    downloader_mock: MagicMock,
    reddit_instance: praw.Reddit,
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
):
    add_console_handler()
    downloader_mock.reddit_instance = reddit_instance
    downloader_mock.download_filter.check_url.return_value = True
    downloader_mock.args.folder_scheme = ""
    downloader_mock.args.max_score = max_score
    downloader_mock.file_name_formatter = RedditConnector.create_file_name_formatter(downloader_mock)
    downloader_mock.download_directory = tmp_path
    submission = downloader_mock.reddit_instance.submission(id=test_submission_id)
    RedditDownloader._download_submission(downloader_mock, submission)
    output = capsys.readouterr()
    assert "filtered due to score" not in output.out


@pytest.mark.online
@pytest.mark.reddit
@pytest.mark.parametrize(("test_submission_id", "max_score"), (("ljyy27", 1),))
def test_download_submission_max_score_above(
    test_submission_id: str,
    max_score: int,
    downloader_mock: MagicMock,
    reddit_instance: praw.Reddit,
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
):
    add_console_handler()
    downloader_mock.reddit_instance = reddit_instance
    downloader_mock.download_filter.check_url.return_value = True
    downloader_mock.args.folder_scheme = ""
    downloader_mock.args.max_score = max_score
    downloader_mock.file_name_formatter = RedditConnector.create_file_name_formatter(downloader_mock)
    downloader_mock.download_directory = tmp_path
    submission = downloader_mock.reddit_instance.submission(id=test_submission_id)
    RedditDownloader._download_submission(downloader_mock, submission)
    output = capsys.readouterr()
    assert "filtered due to score" in output.out


def _make_fake_submission(submission_id: str, url: str = "https://example.com/a.png"):
    """A submission stand-in whose attributes are all already materialised."""
    submission = MagicMock()
    submission.id = submission_id
    submission.url = url
    submission.author.name = "test_author"
    submission.subreddit.display_name = "test_subreddit"
    submission.score = 1000
    submission.upvote_ratio = 0.9
    submission.created_utc = 1600000000.0
    submission.__class__ = praw.models.Submission
    return submission


def _prepare_downloader_for_offline_download(downloader_mock: MagicMock, tmp_path: Path):
    downloader_mock.excluded_submission_ids = set()
    downloader_mock.args.skip_subreddit = set()
    downloader_mock.args.ignore_user = []
    downloader_mock.args.disable_module = set()
    downloader_mock.args.no_dupes = False
    downloader_mock.args.make_hard_links = False
    downloader_mock.args.max_wait_time = 1
    downloader_mock.download_filter.check_url.return_value = True
    downloader_mock.download_filter.check_resource.return_value = True
    downloader_mock.download_directory = tmp_path
    downloader_mock.master_hash_list = {}


@patch("bdfr.downloader.DownloadFactory.pull_lever")
def test_worker_stage_makes_no_reddit_api_calls(mock_pull_lever: MagicMock, downloader_mock: MagicMock, tmp_path: Path):
    """The worker stage must never touch the Reddit instance.

    PRAW is not thread-safe, so any Reddit access from a worker thread is a
    correctness bug. Resource fetching and disk writes must be Reddit-free.
    """
    _prepare_downloader_for_offline_download(downloader_mock, tmp_path)
    submission = _make_fake_submission("aaaaaa")

    resource = MagicMock()
    resource.url = "https://example.com/a.png"
    resource.content = b"payload"
    resource.hash.hexdigest.return_value = "deadbeef"
    downloader_mock.file_name_formatter.format_resource_paths.return_value = [(tmp_path / "a.png", resource)]

    downloader_class = MagicMock()
    downloader_class.__name__ = "Direct"
    mock_pull_lever.return_value = downloader_class

    reddit_instance = MagicMock()
    downloader_mock.reddit_instance = reddit_instance

    RedditDownloader._download_submission_resources(downloader_mock, submission)

    assert (tmp_path / "a.png").read_bytes() == b"payload"
    assert reddit_instance.mock_calls == [], f"worker touched the Reddit instance: {reddit_instance.mock_calls}"


@patch("bdfr.downloader.DownloadFactory.pull_lever")
def test_concurrent_download_processes_every_submission(
    mock_pull_lever: MagicMock, downloader_mock: MagicMock, tmp_path: Path
):
    _prepare_downloader_for_offline_download(downloader_mock, tmp_path)
    submission_count = 25
    submissions = [_make_fake_submission(f"id{i:04d}") for i in range(submission_count)]

    def format_paths(resources, _directory):
        return [(tmp_path / f"{res.url.rsplit('/', 1)[-1]}", res) for res in resources]

    downloader_mock.file_name_formatter.format_resource_paths.side_effect = format_paths

    downloader_class = MagicMock()
    downloader_class.__name__ = "Direct"
    mock_pull_lever.return_value = downloader_class

    def find_resources(_authenticator):
        # One unique resource per call, matching the submission being handled.
        resource = MagicMock()
        resource.url = f"https://example.com/{next(counter)}.png"
        resource.content = b"payload"
        resource.hash.hexdigest.return_value = resource.url
        return [resource]

    counter = iter(range(submission_count))
    downloader_class.return_value.find_resources.side_effect = find_resources

    downloader_mock.concurrency = 5
    downloader_mock.reddit_lists = [submissions]
    RedditDownloader.download(downloader_mock)

    written = sorted(p.name for p in tmp_path.iterdir())
    assert len(written) == submission_count, written


@patch("bdfr.downloader.DownloadFactory.pull_lever")
def test_duplicate_destination_is_written_once(mock_pull_lever: MagicMock, downloader_mock: MagicMock, tmp_path: Path):
    """Two submissions formatting to the same path must not both write it."""
    _prepare_downloader_for_offline_download(downloader_mock, tmp_path)
    destination = tmp_path / "same.png"
    write_attempts = []

    resource = MagicMock()
    resource.url = "https://example.com/same.png"
    resource.content = b"payload"
    resource.hash.hexdigest.return_value = "samehash"
    resource.download.side_effect = lambda *_a, **_k: write_attempts.append(1)
    downloader_mock.file_name_formatter.format_resource_paths.return_value = [(destination, resource)]

    downloader_class = MagicMock()
    downloader_class.__name__ = "Direct"
    mock_pull_lever.return_value = downloader_class

    # First pass writes the file; the second must see it already exists.
    RedditDownloader._download_submission_resources(downloader_mock, _make_fake_submission("aaaaaa"))
    RedditDownloader._download_submission_resources(downloader_mock, _make_fake_submission("bbbbbb"))

    assert destination.read_bytes() == b"payload"
    assert len(write_attempts) == 1, "the same destination was downloaded twice"


def test_claim_destination_is_exclusive(downloader_mock: MagicMock, tmp_path: Path):
    downloader_mock._in_flight_destinations = set()
    downloader_mock._in_flight_lock = threading.Lock()
    target = tmp_path / "x.png"

    assert RedditDownloader._claim_destination(downloader_mock, target) is True
    assert RedditDownloader._claim_destination(downloader_mock, target) is False
    RedditDownloader._release_destination(downloader_mock, target)
    assert RedditDownloader._claim_destination(downloader_mock, target) is True


@patch("bdfr.downloader.DownloadFactory.pull_lever")
def test_failed_write_does_not_poison_hash_list(mock_pull_lever: MagicMock, downloader_mock: MagicMock, tmp_path: Path):
    """A write failure must not leave a hash claiming a file that does not exist."""
    _prepare_downloader_for_offline_download(downloader_mock, tmp_path)
    # A directory as the destination makes the write fail with OSError.
    destination = tmp_path / "blocked"
    destination.mkdir()

    resource = MagicMock()
    resource.url = "https://example.com/a.png"
    resource.content = b"payload"
    resource.hash.hexdigest.return_value = "hash-a"
    downloader_mock.file_name_formatter.format_resource_paths.return_value = [(destination, resource)]

    downloader_class = MagicMock()
    downloader_class.__name__ = "Direct"
    mock_pull_lever.return_value = downloader_class

    RedditDownloader._download_submission_resources(downloader_mock, _make_fake_submission("aaaaaa"))

    assert "hash-a" not in downloader_mock.master_hash_list


class _IdOnlySubmission:
    """A listing submission for which reading anything but the ID could cost a Reddit request."""

    def __init__(self, submission_id: str):
        self.id = submission_id

    def __getattr__(self, name: str):
        raise AssertionError(f"read {name!r}, which a listing submission may fetch from Reddit")


def _use_record(downloader_mock: MagicMock, tmp_path: Path, *recorded_ids: str) -> DownloadRecord:
    record = DownloadRecord(tmp_path / "record" / "record.txt")
    for submission_id in recorded_ids:
        record.add(submission_id)
    downloader_mock.download_record = record
    return record


def _single_resource_download(downloader_mock: MagicMock, mock_pull_lever: MagicMock, tmp_path: Path):
    """Set up a submission with one image, as the Direct downloader would find it."""
    resource = MagicMock()
    resource.url = "https://example.com/a.png"
    resource.content = b"payload"
    resource.hash.hexdigest.return_value = "hash-a"
    destination = tmp_path / "out" / "a.png"
    downloader_mock.file_name_formatter.format_resource_paths.return_value = [(destination, resource)]
    downloader_class = MagicMock()
    downloader_class.__name__ = "Direct"
    downloader_class.return_value.find_resources.return_value = [resource]
    downloader_class.return_value.incomplete = False
    mock_pull_lever.return_value = downloader_class
    return downloader_class, resource, destination


@patch("bdfr.downloader.DownloadFactory.pull_lever")
def test_recorded_submission_is_skipped_before_any_network_call(
    mock_pull_lever: MagicMock, downloader_mock: MagicMock, tmp_path: Path
):
    _prepare_downloader_for_offline_download(downloader_mock, tmp_path)
    _use_record(downloader_mock, tmp_path, "aaaaaa")

    # Anything but the ID would raise, so the skip provably needs no Reddit data.
    RedditDownloader._download_submission(downloader_mock, _IdOnlySubmission("aaaaaa"))

    mock_pull_lever.assert_not_called()
    assert downloader_mock._already_downloaded_skips == 1


@patch("bdfr.downloader.DownloadFactory.pull_lever")
def test_unrecorded_submission_is_downloaded(mock_pull_lever: MagicMock, downloader_mock: MagicMock, tmp_path: Path):
    _prepare_downloader_for_offline_download(downloader_mock, tmp_path)
    _use_record(downloader_mock, tmp_path, "aaaaaa")
    _, _, destination = _single_resource_download(downloader_mock, mock_pull_lever, tmp_path)

    RedditDownloader._download_submission(downloader_mock, _make_fake_submission("bbbbbb"))

    assert destination.read_bytes() == b"payload"
    assert "bbbbbb" in downloader_mock.download_record
    assert downloader_mock._already_downloaded_skips == 0


def _outcome_written(downloader_mock, resource, destination, tmp_path):
    pass


def _outcome_already_exists(downloader_mock, resource, destination, tmp_path):
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"from an earlier run")


def _outcome_filtered(downloader_mock, resource, destination, tmp_path):
    downloader_mock.download_filter.check_resource.return_value = False


def _outcome_duplicate(downloader_mock, resource, destination, tmp_path):
    original = tmp_path / "elsewhere.png"
    original.write_bytes(b"payload")
    downloader_mock.args.no_dupes = True
    downloader_mock.master_hash_list = {"hash-a": original}


def _outcome_hard_linked(downloader_mock, resource, destination, tmp_path):
    original = tmp_path / "elsewhere.png"
    original.write_bytes(b"payload")
    downloader_mock.args.make_hard_links = True
    downloader_mock.master_hash_list = {"hash-a": original}


@pytest.mark.parametrize(
    "arrange",
    (_outcome_written, _outcome_already_exists, _outcome_filtered, _outcome_duplicate, _outcome_hard_linked),
)
@patch("bdfr.downloader.DownloadFactory.pull_lever")
def test_fully_handled_submission_is_recorded(
    mock_pull_lever: MagicMock, arrange, downloader_mock: MagicMock, tmp_path: Path
):
    _prepare_downloader_for_offline_download(downloader_mock, tmp_path)
    record = _use_record(downloader_mock, tmp_path)
    _, resource, destination = _single_resource_download(downloader_mock, mock_pull_lever, tmp_path)
    arrange(downloader_mock, resource, destination, tmp_path)

    RedditDownloader._download_submission_resources(downloader_mock, _make_fake_submission("aaaaaa"))

    assert "aaaaaa" in record
    assert "aaaaaa" in DownloadRecord(record.path), "the record must reach the disk"


def _failure_not_downloadable(downloader_mock, mock_pull_lever, downloader_class, resource, destination):
    mock_pull_lever.side_effect = errors.NotADownloadableLinkError("no downloader for this link")


def _failure_disabled_module(downloader_mock, mock_pull_lever, downloader_class, resource, destination):
    downloader_mock.args.disable_module = {"direct"}


def _failure_find_resources(downloader_mock, mock_pull_lever, downloader_class, resource, destination):
    downloader_class.return_value.find_resources.side_effect = errors.SiteDownloaderError("host said no")


def _failure_nothing_found(downloader_mock, mock_pull_lever, downloader_class, resource, destination):
    downloader_class.return_value.find_resources.return_value = []
    downloader_mock.file_name_formatter.format_resource_paths.return_value = []


def _failure_unnameable_resource(downloader_mock, mock_pull_lever, downloader_class, resource, destination):
    downloader_mock.file_name_formatter.format_resource_paths.return_value = []


def _failure_site_reports_missing_media(downloader_mock, mock_pull_lever, downloader_class, resource, destination):
    downloader_class.return_value.incomplete = True


def _failure_download(downloader_mock, mock_pull_lever, downloader_class, resource, destination):
    resource.download.side_effect = errors.SiteDownloaderError("connection reset")


def _failure_hard_link(downloader_mock, mock_pull_lever, downloader_class, resource, destination):
    downloader_mock.args.make_hard_links = True
    downloader_mock.master_hash_list = {"hash-a": destination.parent / "vanished.png"}


def _failure_duplicate_of_unwritten_file(downloader_mock, mock_pull_lever, downloader_class, resource, destination):
    # The first copy of this content was reserved but never reached the disk.
    downloader_mock.args.no_dupes = True
    downloader_mock.master_hash_list = {"hash-a": destination.parent / "never-written.png"}


def _failure_claimed_by_another_worker(downloader_mock, mock_pull_lever, downloader_class, resource, destination):
    downloader_mock._in_flight_destinations.add(destination)


@pytest.mark.parametrize(
    "arrange",
    (
        _failure_not_downloadable,
        _failure_disabled_module,
        _failure_find_resources,
        _failure_nothing_found,
        _failure_unnameable_resource,
        _failure_site_reports_missing_media,
        _failure_download,
        _failure_hard_link,
        _failure_duplicate_of_unwritten_file,
        _failure_claimed_by_another_worker,
    ),
)
@patch("bdfr.downloader.DownloadFactory.pull_lever")
def test_submission_that_did_not_fully_succeed_is_not_recorded(
    mock_pull_lever: MagicMock, arrange, downloader_mock: MagicMock, tmp_path: Path
):
    _prepare_downloader_for_offline_download(downloader_mock, tmp_path)
    record = _use_record(downloader_mock, tmp_path)
    downloader_class, resource, destination = _single_resource_download(downloader_mock, mock_pull_lever, tmp_path)
    arrange(downloader_mock, mock_pull_lever, downloader_class, resource, destination)

    RedditDownloader._download_submission_resources(downloader_mock, _make_fake_submission("aaaaaa"))

    assert "aaaaaa" not in record
    assert not record.path.exists()


@patch("bdfr.downloader.atomic_write_bytes", side_effect=OSError("disk full"))
@patch("bdfr.downloader.DownloadFactory.pull_lever")
def test_write_error_is_not_recorded(
    mock_pull_lever: MagicMock, _mock_write: MagicMock, downloader_mock: MagicMock, tmp_path: Path
):
    _prepare_downloader_for_offline_download(downloader_mock, tmp_path)
    record = _use_record(downloader_mock, tmp_path)
    _single_resource_download(downloader_mock, mock_pull_lever, tmp_path)

    RedditDownloader._download_submission_resources(downloader_mock, _make_fake_submission("aaaaaa"))

    assert "aaaaaa" not in record


@patch("bdfr.downloader.DownloadFactory.pull_lever")
def test_unexpected_error_is_not_recorded(mock_pull_lever: MagicMock, downloader_mock: MagicMock, tmp_path: Path):
    """An exception the worker does not handle ends the submission before it could be recorded."""
    _prepare_downloader_for_offline_download(downloader_mock, tmp_path)
    record = _use_record(downloader_mock, tmp_path)
    _, resource, _ = _single_resource_download(downloader_mock, mock_pull_lever, tmp_path)
    resource.hash = None

    with pytest.raises(AttributeError):
        RedditDownloader._download_submission_resources(downloader_mock, _make_fake_submission("aaaaaa"))

    assert "aaaaaa" not in record


@patch("bdfr.downloader.DownloadFactory.pull_lever")
def test_one_failed_resource_keeps_a_gallery_unrecorded(
    mock_pull_lever: MagicMock, downloader_mock: MagicMock, tmp_path: Path
):
    _prepare_downloader_for_offline_download(downloader_mock, tmp_path)
    record = _use_record(downloader_mock, tmp_path)
    downloader_class, good, _ = _single_resource_download(downloader_mock, mock_pull_lever, tmp_path)
    bad = MagicMock()
    bad.url = "https://example.com/b.png"
    bad.download.side_effect = errors.SiteDownloaderError("gone")
    downloader_class.return_value.find_resources.return_value = [good, bad]
    downloader_mock.file_name_formatter.format_resource_paths.return_value = [
        (tmp_path / "out" / "a_1.png", good),
        (tmp_path / "out" / "a_2.png", bad),
    ]

    RedditDownloader._download_submission_resources(downloader_mock, _make_fake_submission("aaaaaa"))

    assert (tmp_path / "out" / "a_1.png").exists()
    assert "aaaaaa" not in record


@patch("bdfr.downloader.DownloadFactory.pull_lever")
def test_recheck_looks_at_recorded_posts_and_keeps_recording(
    mock_pull_lever: MagicMock, downloader_mock: MagicMock, tmp_path: Path
):
    """A file deleted by hand only comes back with --recheck."""
    _prepare_downloader_for_offline_download(downloader_mock, tmp_path)
    record = _use_record(downloader_mock, tmp_path, "aaaaaa")
    downloader_mock.args.recheck = True
    _, _, destination = _single_resource_download(downloader_mock, mock_pull_lever, tmp_path)

    RedditDownloader._download_submission(downloader_mock, _make_fake_submission("aaaaaa"))
    RedditDownloader._download_submission(downloader_mock, _make_fake_submission("bbbbbb"))

    assert mock_pull_lever.call_count == 2
    assert destination.read_bytes() == b"payload"
    assert downloader_mock._already_downloaded_skips == 0
    assert "aaaaaa" in record
    assert "bbbbbb" in record
    assert record.path.read_text(encoding="ascii").splitlines() == ["aaaaaa", "bbbbbb"]


@pytest.mark.parametrize("concurrency", (1, 4))
@patch("bdfr.downloader.DownloadFactory.pull_lever")
def test_download_summarises_skipped_posts(
    mock_pull_lever: MagicMock,
    concurrency: int,
    downloader_mock: MagicMock,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    _prepare_downloader_for_offline_download(downloader_mock, tmp_path)
    _use_record(downloader_mock, tmp_path, "aaaaaa", "bbbbbb")
    _single_resource_download(downloader_mock, mock_pull_lever, tmp_path)
    downloader_mock._download_submission = lambda submission: RedditDownloader._download_submission(
        downloader_mock, submission
    )
    downloader_mock._download_sequentially = lambda: RedditDownloader._download_sequentially(downloader_mock)
    downloader_mock.concurrency = concurrency
    downloader_mock.reddit_lists = [
        [_IdOnlySubmission("aaaaaa"), _IdOnlySubmission("bbbbbb"), _make_fake_submission("cccccc")]
    ]
    caplog.set_level(logging.INFO)

    RedditDownloader.download(downloader_mock)

    assert mock_pull_lever.call_count == 1
    summaries = [r for r in caplog.records if getattr(r, "bdfr_event", None) == "run_summary"]
    assert [r.getMessage() for r in summaries] == [
        "Skipped 2 posts already downloaded (use --recheck to verify them again)"
    ]
    if concurrency > 1:
        assert "1 submissions processed, 0 filtered out, 0 failed" in caplog.text


def test_download_without_skips_logs_no_summary(downloader_mock: MagicMock, caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.INFO)
    RedditDownloader._log_already_downloaded_summary(downloader_mock)
    assert "already downloaded" not in caplog.text


def _build_without_reddit(cls: type, args: Configuration, directory: Path):
    """Run the real constructors of the download classes with the Reddit setup left out."""

    def connector_init(self, args, logging_handlers=()):
        self.args = args
        self.download_directory = directory
        self.master_hash_list = {}
        self.excluded_submission_ids = set()
        self.download_filter = DownloadFilter(args.skip, args.skip_domain)

    with patch.object(RedditConnector, "__init__", connector_init):
        return cls(args)


@pytest.mark.parametrize(("cls", "skips_recorded"), ((RedditDownloader, True), (RedditCloner, False)))
def test_only_the_downloader_skips_recorded_posts(cls: type, skips_recorded: bool, args: Configuration, tmp_path: Path):
    """The cloner also writes an archive entry per post, which a skip would leave out."""
    args.skip_subreddit = set()
    DownloadRecord.for_settings(tmp_path, record_settings(args)).add("aaaaaa")

    instance = _build_without_reddit(cls, args, tmp_path)

    assert (instance.download_record is not None) is skips_recorded
    assert instance._should_download_submission(_make_fake_submission("aaaaaa")) is not skips_recorded


def test_downloader_opens_the_record_for_its_own_settings(args: Configuration, tmp_path: Path):
    args.skip = ["mp4"]
    instance = _build_without_reddit(RedditDownloader, args, tmp_path)
    expected = DownloadRecord.for_settings(tmp_path, record_settings(args))
    assert instance.download_record.path == expected.path


def test_search_existing_leaves_out_bdfr_state(tmp_path: Path):
    (tmp_path / "media.png").write_bytes(b"image")
    state = tmp_path / STATE_DIRECTORY_NAME / "records"
    state.mkdir(parents=True)
    (state / "abc.txt").write_text("aaaaaa\n", encoding="ascii")

    hashes = RedditDownloader.scan_existing_files(tmp_path)

    assert list(hashes.values()) == [tmp_path / "media.png"]


@pytest.mark.parametrize(("argv", "expected"), ((["some_dir"], False), (["some_dir", "--recheck"], True)))
def test_recheck_flag_reaches_the_configuration(argv: list[str], expected: bool):
    config = Configuration()
    config.process_click_arguments(cli_download.make_context("download", argv))
    assert config.recheck is expected


def _duplicate_pair(downloader_mock: MagicMock, tmp_path: Path):
    """Two resources with identical content bound for different destinations."""
    downloader_mock.args.no_dupes = True
    downloader_mock.args.max_wait_time = 1
    resources = []
    for name in ("first", "second"):
        resource = MagicMock()
        resource.url = f"https://example.com/{name}.png"
        resource.content = b"identical payload"
        resource.hash.hexdigest.return_value = "shared-hash"
        resources.append((tmp_path / f"{name}.png", resource))
    return resources


def test_duplicate_waits_for_the_first_copy_to_be_written(downloader_mock: MagicMock, tmp_path: Path):
    """A duplicate must not count as done while the first copy is still being written."""
    (first_path, first), (second_path, second) = _duplicate_pair(downloader_mock, tmp_path)
    release_write = threading.Event()
    write_started = threading.Event()
    results = {}

    def slow_write(destination, content):
        write_started.set()
        release_write.wait(timeout=30)
        destination.write_bytes(content)

    with patch("bdfr.downloader.atomic_write_bytes", side_effect=slow_write):
        first_worker = threading.Thread(
            target=lambda: results.__setitem__(
                "first",
                RedditDownloader._write_resource(
                    downloader_mock, _make_fake_submission("aaaaaa"), first_path, first, "Direct"
                ),
            )
        )
        first_worker.start()
        assert write_started.wait(timeout=30)
        second_worker = threading.Thread(
            target=lambda: results.__setitem__(
                "second",
                RedditDownloader._write_resource(
                    downloader_mock, _make_fake_submission("bbbbbb"), second_path, second, "Direct"
                ),
            )
        )
        second_worker.start()
        second_worker.join(timeout=0.5)
        assert second_worker.is_alive(), "the duplicate decided before the first copy existed"
        release_write.set()
        first_worker.join(timeout=30)
        second_worker.join(timeout=30)

    assert results == {"first": True, "second": True}
    assert first_path.read_bytes() == b"identical payload"
    assert not second_path.exists()


def test_duplicate_of_a_failed_write_is_not_done(downloader_mock: MagicMock, tmp_path: Path):
    """If the first copy fails to write, the duplicate must be retried, not recorded."""
    (first_path, first), (second_path, second) = _duplicate_pair(downloader_mock, tmp_path)
    release_write = threading.Event()
    write_started = threading.Event()
    results = {}

    def failing_write(destination, content):
        write_started.set()
        release_write.wait(timeout=30)
        raise OSError("disk full")

    with patch("bdfr.downloader.atomic_write_bytes", side_effect=failing_write):
        first_worker = threading.Thread(
            target=lambda: results.__setitem__(
                "first",
                RedditDownloader._write_resource(
                    downloader_mock, _make_fake_submission("aaaaaa"), first_path, first, "Direct"
                ),
            )
        )
        first_worker.start()
        assert write_started.wait(timeout=30)
        second_worker = threading.Thread(
            target=lambda: results.__setitem__(
                "second",
                RedditDownloader._write_resource(
                    downloader_mock, _make_fake_submission("bbbbbb"), second_path, second, "Direct"
                ),
            )
        )
        second_worker.start()
        release_write.set()
        first_worker.join(timeout=30)
        second_worker.join(timeout=30)

    assert results == {"first": False, "second": False}
    assert "shared-hash" not in downloader_mock.master_hash_list


def test_duplicate_of_a_preexisting_file_needs_no_wait(downloader_mock: MagicMock, tmp_path: Path):
    """Hashes from --search-existing have no pending write and must not block."""
    existing = tmp_path / "scanned.png"
    existing.write_bytes(b"identical payload")
    (_, _), (second_path, second) = _duplicate_pair(downloader_mock, tmp_path)
    downloader_mock.master_hash_list = {"shared-hash": existing}

    handled = RedditDownloader._write_resource(
        downloader_mock, _make_fake_submission("bbbbbb"), second_path, second, "Direct"
    )

    assert handled is True
    assert not second_path.exists()


def test_duplicate_with_unknown_location_still_counts_as_elsewhere(downloader_mock: MagicMock, tmp_path: Path):
    """A hash list entry without a path keeps the historical no-dupes behaviour."""
    (_, _), (second_path, second) = _duplicate_pair(downloader_mock, tmp_path)
    downloader_mock.master_hash_list = {"shared-hash": None}

    handled = RedditDownloader._write_resource(
        downloader_mock, _make_fake_submission("bbbbbb"), second_path, second, "Direct"
    )

    assert handled is True
    assert not second_path.exists()
