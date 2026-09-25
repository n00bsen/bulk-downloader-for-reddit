#!/usr/bin/env python3

import configparser
import io
import json
import logging
import os
import platform
import socket
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, call
from urllib.parse import urlparse

import praw
import praw.models
import prawcore
import pytest
import requests

from bdfr import __version__
from bdfr.archiver import Archiver
from bdfr.configuration import Configuration
from bdfr.connector import (
    RedditConnector,
    RedditTypes,
    build_user_agent,
    describe_reddit_client,
    raise_if_credentials_rejected,
    resolve_reddit_credentials,
)
from bdfr.download_filter import DownloadFilter
from bdfr.exceptions import BulkDownloaderException, RedditAuthenticationError
from bdfr.file_name_formatter import FileNameFormatter
from bdfr.listing import DEFAULT_RATE_LIMIT_WAIT, MAX_ATTEMPTS, RATE_LIMIT_MARGIN, ResumableListing
from bdfr.oauth2 import OAuth2TokenManager
from bdfr.site_authenticator import SiteAuthenticator


@pytest.fixture()
def args() -> Configuration:
    args = Configuration()
    args.time_format = "ISO"
    return args


@pytest.fixture()
def downloader_mock(args: Configuration):
    downloader_mock = MagicMock()
    downloader_mock.args = args
    downloader_mock.sanitise_subreddit_name = RedditConnector.sanitise_subreddit_name
    downloader_mock.create_filtered_listing_generator = lambda *args, **kwargs: (
        RedditConnector.create_filtered_listing_generator(downloader_mock, *args, **kwargs)
    )
    downloader_mock.split_args_input = RedditConnector.split_args_input
    downloader_mock.master_hash_list = {}
    return downloader_mock


def assert_all_results_are_submissions(result_limit: int, results: list[Iterator]) -> list:
    results = [sub for res in results for sub in res]
    assert all(isinstance(res, praw.models.Submission) for res in results)
    assert not any(isinstance(m, MagicMock) for m in results)
    if result_limit is not None:
        assert len(results) == result_limit
    return results


def assert_all_results_are_submissions_or_comments(result_limit: int, results: list[Iterator]) -> list:
    results = [sub for res in results for sub in res]
    assert all(isinstance(res, (praw.models.Submission, praw.models.Comment)) for res in results)
    assert not any(isinstance(m, MagicMock) for m in results)
    if result_limit is not None:
        assert len(results) == result_limit
    return results


def test_determine_directories(tmp_path: Path, downloader_mock: MagicMock):
    downloader_mock.args.directory = tmp_path / "test"
    downloader_mock.config_directories.user_config_dir = tmp_path
    RedditConnector.determine_directories(downloader_mock)
    assert Path(tmp_path / "test").exists()


@pytest.mark.parametrize(
    ("skip_extensions", "skip_domains"),
    (
        ([], []),
        (
            [".test"],
            ["test.com"],
        ),
    ),
)
def test_create_download_filter(skip_extensions: list[str], skip_domains: list[str], downloader_mock: MagicMock):
    downloader_mock.args.skip = skip_extensions
    downloader_mock.args.skip_domain = skip_domains
    result = RedditConnector.create_download_filter(downloader_mock)

    assert isinstance(result, DownloadFilter)
    assert result.excluded_domains == skip_domains
    assert result.excluded_extensions == skip_extensions


@pytest.mark.parametrize(
    ("test_time", "expected"),
    (
        ("all", "all"),
        ("hour", "hour"),
        ("day", "day"),
        ("week", "week"),
        ("random", "all"),
        ("", "all"),
    ),
)
def test_create_time_filter(test_time: str, expected: str, downloader_mock: MagicMock):
    downloader_mock.args.time = test_time
    result = RedditConnector.create_time_filter(downloader_mock)

    assert isinstance(result, RedditTypes.TimeType)
    assert result.name.lower() == expected


@pytest.mark.parametrize(
    ("test_sort", "expected"),
    (
        ("", "hot"),
        ("hot", "hot"),
        ("controversial", "controversial"),
        ("new", "new"),
    ),
)
def test_create_sort_filter(test_sort: str, expected: str, downloader_mock: MagicMock):
    downloader_mock.args.sort = test_sort
    result = RedditConnector.create_sort_filter(downloader_mock)

    assert isinstance(result, RedditTypes.SortType)
    assert result.name.lower() == expected


@pytest.mark.parametrize(
    ("test_file_scheme", "test_folder_scheme"),
    (
        ("{POSTID}", "{SUBREDDIT}"),
        ("{REDDITOR}_{TITLE}_{POSTID}", "{SUBREDDIT}"),
        ("{POSTID}", "test"),
        ("{POSTID}", ""),
        ("{POSTID}", "{SUBREDDIT}/{REDDITOR}"),
    ),
)
def test_create_file_name_formatter(test_file_scheme: str, test_folder_scheme: str, downloader_mock: MagicMock):
    downloader_mock.args.file_scheme = test_file_scheme
    downloader_mock.args.folder_scheme = test_folder_scheme
    result = RedditConnector.create_file_name_formatter(downloader_mock)

    assert isinstance(result, FileNameFormatter)
    assert result.file_format_string == test_file_scheme
    assert result.directory_format_string == test_folder_scheme.split("/")


@pytest.mark.parametrize(
    ("test_file_scheme", "test_folder_scheme"),
    (
        ("", ""),
        ("", "{SUBREDDIT}"),
        ("test", "{SUBREDDIT}"),
    ),
)
def test_create_file_name_formatter_bad(test_file_scheme: str, test_folder_scheme: str, downloader_mock: MagicMock):
    downloader_mock.args.file_scheme = test_file_scheme
    downloader_mock.args.folder_scheme = test_folder_scheme
    with pytest.raises(BulkDownloaderException):
        RedditConnector.create_file_name_formatter(downloader_mock)


def test_create_authenticator(downloader_mock: MagicMock):
    result = RedditConnector.create_authenticator(downloader_mock)
    assert isinstance(result, SiteAuthenticator)


@pytest.mark.online
@pytest.mark.reddit
@pytest.mark.parametrize(
    "test_submission_ids",
    (
        ("lvpf4l",),
        ("lvpf4l", "lvqnsn"),
        ("lvpf4l", "lvqnsn", "lvl9kd"),
        ("1000000",),
    ),
)
def test_get_submissions_from_link(
    test_submission_ids: list[str], reddit_instance: praw.Reddit, downloader_mock: MagicMock
):
    downloader_mock.args.link = test_submission_ids
    downloader_mock.reddit_instance = reddit_instance
    results = RedditConnector.get_submissions_from_link(downloader_mock)
    assert all(isinstance(sub, praw.models.Submission) for res in results for sub in res)
    assert len(results[0]) == len(test_submission_ids)


@pytest.mark.online
@pytest.mark.reddit
@pytest.mark.parametrize(
    ("test_subreddits", "limit", "sort_type", "time_filter", "max_expected_len"),
    (
        (("Futurology",), 10, "hot", "all", 10),
        (("Futurology", "Mindustry, Python"), 10, "hot", "all", 30),
        (("Futurology",), 20, "hot", "all", 20),
        (("Futurology", "Python"), 10, "hot", "all", 20),
        (("Futurology",), 100, "hot", "all", 100),
        (("Futurology",), 0, "hot", "all", 0),
        (("Futurology",), 10, "top", "all", 10),
        (("Futurology",), 10, "top", "week", 10),
        (("Futurology",), 10, "hot", "week", 10),
    ),
)
def test_get_subreddit_normal(
    test_subreddits: list[str],
    limit: int,
    sort_type: str,
    time_filter: str,
    max_expected_len: int,
    downloader_mock: MagicMock,
    reddit_instance: praw.Reddit,
):
    downloader_mock.args.limit = limit
    downloader_mock.args.sort = sort_type
    downloader_mock.time_filter = RedditConnector.create_time_filter(downloader_mock)
    downloader_mock.sort_filter = RedditConnector.create_sort_filter(downloader_mock)
    downloader_mock.determine_sort_function.return_value = RedditConnector.determine_sort_function(downloader_mock)
    downloader_mock.args.subreddit = test_subreddits
    downloader_mock.reddit_instance = reddit_instance
    results = RedditConnector.get_subreddits(downloader_mock)
    test_subreddits = downloader_mock.split_args_input(test_subreddits)
    results = [sub for res1 in results for sub in res1]
    assert all(isinstance(res1, praw.models.Submission) for res1 in results)
    assert all(res.subreddit.display_name in test_subreddits for res in results)
    assert len(results) <= max_expected_len
    assert not any(isinstance(m, MagicMock) for m in results)


@pytest.mark.online
@pytest.mark.reddit
@pytest.mark.parametrize(
    ("test_time", "test_delta"),
    (
        ("hour", timedelta(hours=1)),
        ("day", timedelta(days=1)),
        ("week", timedelta(days=7)),
        ("month", timedelta(days=31)),
        ("year", timedelta(days=365)),
    ),
)
def test_get_subreddit_time_verification(
    test_time: str,
    test_delta: timedelta,
    downloader_mock: MagicMock,
    reddit_instance: praw.Reddit,
):
    downloader_mock.args.limit = 10
    downloader_mock.args.sort = "top"
    downloader_mock.args.time = test_time
    downloader_mock.time_filter = RedditConnector.create_time_filter(downloader_mock)
    downloader_mock.sort_filter = RedditConnector.create_sort_filter(downloader_mock)
    downloader_mock.determine_sort_function.return_value = RedditConnector.determine_sort_function(downloader_mock)
    downloader_mock.args.subreddit = ["all"]
    downloader_mock.reddit_instance = reddit_instance
    results = RedditConnector.get_subreddits(downloader_mock)
    results = [sub for res1 in results for sub in res1]
    assert all(isinstance(res1, praw.models.Submission) for res1 in results)
    nowtime = datetime.now()
    for r in results:
        result_time = datetime.fromtimestamp(r.created_utc)
        time_diff = nowtime - result_time
        assert time_diff < (test_delta + timedelta(minutes=1))


@pytest.mark.online
@pytest.mark.reddit
@pytest.mark.parametrize(
    ("test_subreddits", "search_term", "limit", "time_filter", "max_expected_len"),
    (
        (("Python",), "scraper", 10, "all", 10),
        (("Python",), "", 10, "all", 0),
        (("Python",), "djsdsgewef", 10, "all", 0),
        (("Python",), "scraper", 10, "year", 10),
    ),
)
def test_get_subreddit_search(
    test_subreddits: list[str],
    search_term: str,
    time_filter: str,
    limit: int,
    max_expected_len: int,
    downloader_mock: MagicMock,
    reddit_instance: praw.Reddit,
):
    downloader_mock._determine_sort_function.return_value = praw.models.Subreddit.hot
    downloader_mock.args.limit = limit
    downloader_mock.args.search = search_term
    downloader_mock.args.subreddit = test_subreddits
    downloader_mock.reddit_instance = reddit_instance
    downloader_mock.sort_filter = RedditTypes.SortType.HOT
    downloader_mock.args.time = time_filter
    downloader_mock.time_filter = RedditConnector.create_time_filter(downloader_mock)
    results = RedditConnector.get_subreddits(downloader_mock)
    results = [sub for res in results for sub in res]
    assert all(isinstance(res, praw.models.Submission) for res in results)
    assert all(res.subreddit.display_name in test_subreddits for res in results)
    assert len(results) <= max_expected_len
    if max_expected_len != 0:
        assert results
    assert not any(isinstance(m, MagicMock) for m in results)


@pytest.mark.online
@pytest.mark.reddit
@pytest.mark.parametrize(
    ("test_user", "test_multireddits", "limit"),
    (
        ("helen_darten", ("cuteanimalpics",), 10),
        ("korfor", ("chess",), 100),
    ),
)
# Good sources at https://www.reddit.com/r/multihub/
def test_get_multireddits_public(
    test_user: str,
    test_multireddits: list[str],
    limit: int,
    reddit_instance: praw.Reddit,
    downloader_mock: MagicMock,
):
    downloader_mock.determine_sort_function.return_value = praw.models.Subreddit.hot
    downloader_mock.sort_filter = RedditTypes.SortType.HOT
    downloader_mock.args.limit = limit
    downloader_mock.args.multireddit = test_multireddits
    downloader_mock.args.user = [test_user]
    downloader_mock.reddit_instance = reddit_instance
    downloader_mock.create_filtered_listing_generator.return_value = RedditConnector.create_filtered_listing_generator(
        downloader_mock,
        reddit_instance.multireddit(redditor=test_user, name=test_multireddits[0]),
    )
    results = RedditConnector.get_multireddits(downloader_mock)
    results = [sub for res in results for sub in res]
    assert all(isinstance(res, praw.models.Submission) for res in results)
    assert len(results) == limit
    assert not any(isinstance(m, MagicMock) for m in results)


@pytest.mark.online
@pytest.mark.reddit
@pytest.mark.parametrize(
    ("test_user", "limit"),
    (
        ("danigirl3694", 10),
        ("danigirl3694", 50),
        ("nasa", None),
    ),
)
def test_get_user_submissions(test_user: str, limit: int, downloader_mock: MagicMock, reddit_instance: praw.Reddit):
    downloader_mock.args.limit = limit
    downloader_mock.determine_sort_function.return_value = praw.models.Subreddit.hot
    downloader_mock.sort_filter = RedditTypes.SortType.HOT
    downloader_mock.args.submitted = True
    downloader_mock.args.user = [test_user]
    downloader_mock.authenticated = False
    downloader_mock.reddit_instance = reddit_instance
    downloader_mock.create_filtered_listing_generator.return_value = RedditConnector.create_filtered_listing_generator(
        downloader_mock,
        reddit_instance.redditor(test_user).submissions,
    )
    results = RedditConnector.get_user_data(downloader_mock)
    results = assert_all_results_are_submissions(limit, results)
    assert all(res.author.name == test_user for res in results)
    assert not any(isinstance(m, MagicMock) for m in results)


@pytest.mark.online
@pytest.mark.reddit
@pytest.mark.authenticated
@pytest.mark.parametrize(
    "test_flag",
    (
        "upvoted",
        "saved",
    ),
)
def test_get_user_authenticated_lists(
    test_flag: str,
    downloader_mock: MagicMock,
    authenticated_reddit_instance: praw.Reddit,
):
    downloader_mock.args.__dict__[test_flag] = True
    downloader_mock.reddit_instance = authenticated_reddit_instance
    downloader_mock.args.limit = 10
    downloader_mock.determine_sort_function.return_value = praw.models.Subreddit.hot
    downloader_mock.sort_filter = RedditTypes.SortType.HOT
    downloader_mock.args.user = [RedditConnector.resolve_user_name(downloader_mock, "me")]
    results = RedditConnector.get_user_data(downloader_mock)
    assert_all_results_are_submissions_or_comments(10, results)


@pytest.mark.online
@pytest.mark.reddit
@pytest.mark.authenticated
def test_get_subscribed_subreddits(downloader_mock: MagicMock, authenticated_reddit_instance: praw.Reddit):
    downloader_mock.reddit_instance = authenticated_reddit_instance
    downloader_mock.args.limit = 10
    downloader_mock.args.authenticate = True
    downloader_mock.args.subscribed = True
    downloader_mock.determine_sort_function.return_value = praw.models.Subreddit.hot
    downloader_mock.determine_sort_function.return_value = praw.models.Subreddit.hot
    downloader_mock.sort_filter = RedditTypes.SortType.HOT
    results = RedditConnector.get_subreddits(downloader_mock)
    assert all(isinstance(s, ResumableListing) for s in results)
    assert results


@pytest.mark.parametrize(
    ("test_name", "expected"),
    (
        ("Mindustry", "Mindustry"),
        ("Futurology", "Futurology"),
        ("r/Mindustry", "Mindustry"),
        ("TrollXChromosomes", "TrollXChromosomes"),
        ("r/TrollXChromosomes", "TrollXChromosomes"),
        ("https://www.reddit.com/r/TrollXChromosomes/", "TrollXChromosomes"),
        ("https://www.reddit.com/r/TrollXChromosomes", "TrollXChromosomes"),
        ("https://www.reddit.com/r/Futurology/", "Futurology"),
        ("https://www.reddit.com/r/Futurology", "Futurology"),
    ),
)
def test_sanitise_subreddit_name(test_name: str, expected: str):
    result = RedditConnector.sanitise_subreddit_name(test_name)
    assert result == expected


@pytest.mark.parametrize(
    ("test_subreddit_entries", "expected"),
    (
        (["test1", "test2", "test3"], {"test1", "test2", "test3"}),
        (["test1,test2", "test3"], {"test1", "test2", "test3"}),
        (["test1, test2", "test3"], {"test1", "test2", "test3"}),
        (["test1; test2", "test3"], {"test1", "test2", "test3"}),
        (["test1, test2", "test1,test2,test3", "test4"], {"test1", "test2", "test3", "test4"}),
        ([""], {""}),
        (["test"], {"test"}),
    ),
)
def test_split_subreddit_entries(test_subreddit_entries: list[str], expected: set[str]):
    results = RedditConnector.split_args_input(test_subreddit_entries)
    assert results == expected


def test_read_submission_ids_from_file(downloader_mock: MagicMock, tmp_path: Path):
    test_file = tmp_path / "test.txt"
    test_file.write_text("aaaaaa\nbbbbbb")
    results = RedditConnector.read_id_files([str(test_file)])
    assert results == {"aaaaaa", "bbbbbb"}


@pytest.mark.online
@pytest.mark.reddit
@pytest.mark.parametrize(
    "test_redditor_name",
    (
        "nasa",
        "crowdstrike",
        "HannibalGoddamnit",
    ),
)
def test_check_user_existence_good(
    test_redditor_name: str,
    reddit_instance: praw.Reddit,
    downloader_mock: MagicMock,
):
    downloader_mock.reddit_instance = reddit_instance
    RedditConnector.check_user_existence(downloader_mock, test_redditor_name)


@pytest.mark.online
@pytest.mark.reddit
@pytest.mark.parametrize(
    "test_redditor_name",
    (
        "lhnhfkuhwreolo",
        "adlkfmnhglojh",
    ),
)
def test_check_user_existence_nonexistent(
    test_redditor_name: str,
    reddit_instance: praw.Reddit,
    downloader_mock: MagicMock,
):
    downloader_mock.reddit_instance = reddit_instance
    with pytest.raises(BulkDownloaderException, match="Could not find"):
        RedditConnector.check_user_existence(downloader_mock, test_redditor_name)


@pytest.mark.online
@pytest.mark.reddit
@pytest.mark.parametrize("test_redditor_name", ("Bree-Boo",))
def test_check_user_existence_banned(
    test_redditor_name: str,
    reddit_instance: praw.Reddit,
    downloader_mock: MagicMock,
):
    downloader_mock.reddit_instance = reddit_instance
    with pytest.raises(BulkDownloaderException, match="is banned"):
        RedditConnector.check_user_existence(downloader_mock, test_redditor_name)


@pytest.mark.online
@pytest.mark.reddit
@pytest.mark.parametrize(
    ("test_subreddit_name", "expected_message"),
    (
        ("donaldtrump", "cannot be found"),
        ("submitters", "private and cannot be scraped"),
        ("lhnhfkuhwreolo", "does not exist"),
    ),
)
def test_check_subreddit_status_bad(test_subreddit_name: str, expected_message: str, reddit_instance: praw.Reddit):
    test_subreddit = reddit_instance.subreddit(test_subreddit_name)
    with pytest.raises(BulkDownloaderException, match=expected_message):
        RedditConnector.check_subreddit_status(test_subreddit)


@pytest.mark.online
@pytest.mark.reddit
@pytest.mark.parametrize(
    "test_subreddit_name",
    (
        "Python",
        "Mindustry",
        "TrollXChromosomes",
        "all",
    ),
)
def test_check_subreddit_status_good(test_subreddit_name: str, reddit_instance: praw.Reddit):
    test_subreddit = reddit_instance.subreddit(test_subreddit_name)
    RedditConnector.check_subreddit_status(test_subreddit)


def test_determine_log_path_defaults_to_per_process_file(downloader_mock: MagicMock, tmp_path: Path):
    """Two simultaneous runs must not share one log file."""
    downloader_mock.args.log = None
    downloader_mock.config_directory = tmp_path
    result = RedditConnector.determine_log_path(downloader_mock)
    assert result.parent == tmp_path
    assert str(os.getpid()) in result.name
    assert downloader_mock.owns_log_file is False


def test_determine_log_path_honours_explicit_log(downloader_mock: MagicMock, tmp_path: Path):
    downloader_mock.args.log = str(tmp_path / "mine.log")
    downloader_mock.config_directory = tmp_path
    result = RedditConnector.determine_log_path(downloader_mock)
    assert result == (tmp_path / "mine.log").resolve()
    assert downloader_mock.owns_log_file is True


def test_determine_log_path_rejects_missing_parent(downloader_mock: MagicMock, tmp_path: Path):
    downloader_mock.args.log = str(tmp_path / "nope" / "mine.log")
    downloader_mock.config_directory = tmp_path
    with pytest.raises(BulkDownloaderException, match="does not exist"):
        RedditConnector.determine_log_path(downloader_mock)


def test_write_config_if_changed_skips_identical_content(downloader_mock: MagicMock, tmp_path: Path):
    """An unchanged config must not be rewritten, so concurrent runs stay clean."""
    config_path = tmp_path / "config.cfg"
    parser = configparser.ConfigParser()
    parser["DEFAULT"] = {"max_wait_time": "120"}
    buffer = io.StringIO()
    parser.write(buffer)
    config_path.write_text(buffer.getvalue(), encoding="utf-8")
    original_mtime = config_path.stat().st_mtime_ns

    downloader_mock.cfg_parser = parser
    downloader_mock.config_location = config_path
    RedditConnector.write_config_if_changed(downloader_mock)

    assert config_path.stat().st_mtime_ns == original_mtime


def test_write_config_if_changed_persists_new_value(downloader_mock: MagicMock, tmp_path: Path):
    config_path = tmp_path / "config.cfg"
    config_path.write_text("[DEFAULT]\n", encoding="utf-8")
    parser = configparser.ConfigParser()
    parser.read(config_path)
    parser["DEFAULT"]["user_token"] = "sentinel-token"

    downloader_mock.cfg_parser = parser
    downloader_mock.config_location = config_path
    RedditConnector.write_config_if_changed(downloader_mock)

    assert "sentinel-token" in config_path.read_text(encoding="utf-8")


def test_write_config_if_changed_matches_the_token_managers_format(
    downloader_mock: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A config the token manager has just saved must count as unchanged.

    With "key=value" here and "key = value" there, each writer undid the other
    and every authenticated run, in every GUI job process, rewrote config.cfg.
    """
    config_path = tmp_path / "config.cfg"
    parser = configparser.ConfigParser()
    parser["DEFAULT"] = {"client_id": "abcdefghijklmnop", "user_token": "old-token"}
    OAuth2TokenManager(parser, config_path).post_refresh_callback(MagicMock(refresh_token="rotated-token"))
    writes = MagicMock()
    monkeypatch.setattr("bdfr.connector.atomic_write", writes)

    downloader_mock.cfg_parser = parser
    downloader_mock.config_location = config_path
    RedditConnector.write_config_if_changed(downloader_mock)

    writes.assert_not_called()
    assert "user_token = rotated-token" in config_path.read_text(encoding="utf-8")


def test_write_config_if_changed_leaves_no_temp_files(downloader_mock: MagicMock, tmp_path: Path):
    config_path = tmp_path / "config.cfg"
    config_path.write_text("[DEFAULT]\n", encoding="utf-8")
    parser = configparser.ConfigParser()
    parser.read(config_path)
    parser["DEFAULT"]["changed"] = "yes"

    downloader_mock.cfg_parser = parser
    downloader_mock.config_location = config_path
    RedditConnector.write_config_if_changed(downloader_mock)

    assert not any(p.name.endswith(".tmp") for p in tmp_path.iterdir())


SECRET_SENTINEL = "sentinel-secret-value-9f3a"
HOSTNAME_SENTINEL = "leaky-hostname-sentinel"


@pytest.fixture()
def bundled_cfg() -> configparser.ConfigParser:
    """The config file exactly as BDFR ships it."""
    parser = configparser.ConfigParser()
    parser.read(Path(__file__).parents[1] / "bdfr" / "default_config.cfg")
    return parser


@pytest.fixture()
def fake_praw_reddit(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Stand in for praw.Reddit so the instance can be inspected without any network access."""
    fake = MagicMock()
    monkeypatch.setattr("bdfr.connector.praw.Reddit", fake)
    return fake


@pytest.fixture()
def no_hostname(monkeypatch: pytest.MonkeyPatch):
    """Make any use of the machine's name show up as a sentinel in the result."""
    monkeypatch.setattr(socket, "gethostname", lambda: HOSTNAME_SENTINEL)
    monkeypatch.setattr(platform, "node", lambda: HOSTNAME_SENTINEL)


def test_resolve_credentials_uses_config_file_by_default(args: Configuration, bundled_cfg: configparser.ConfigParser):
    client_id, client_secret = resolve_reddit_credentials(args, bundled_cfg)
    assert client_id == bundled_cfg.get("DEFAULT", "client_id")
    assert client_secret == bundled_cfg.get("DEFAULT", "client_secret")


def test_resolve_credentials_explicit_arguments_beat_config(
    args: Configuration, bundled_cfg: configparser.ConfigParser
):
    args.client_id = "my-own-client-id"
    args.client_secret = SECRET_SENTINEL
    assert resolve_reddit_credentials(args, bundled_cfg) == ("my-own-client-id", SECRET_SENTINEL)


def test_resolve_credentials_explicit_id_never_borrows_config_secret(
    args: Configuration, bundled_cfg: configparser.ConfigParser
):
    """The bundled secret belongs to the bundled ID; paired with another ID it can only fail."""
    args.client_id = "my-own-client-id"
    assert resolve_reddit_credentials(args, bundled_cfg) == ("my-own-client-id", None)


@pytest.fixture()
def own_app_cfg() -> configparser.ConfigParser:
    """A config.cfg into which the user already put their own script app, as the old README said to."""
    parser = configparser.ConfigParser()
    parser["DEFAULT"] = {"client_id": "my-script-app", "client_secret": SECRET_SENTINEL}
    return parser


def test_resolve_credentials_same_id_as_config_keeps_config_secret(
    args: Configuration, own_app_cfg: configparser.ConfigParser
):
    """Naming the ID config.cfg already holds must not turn a script app into a secretless installed app."""
    args.client_id = " my-script-app "
    assert resolve_reddit_credentials(args, own_app_cfg) == ("my-script-app", SECRET_SENTINEL)


def test_resolve_credentials_empty_secret_forces_installed_app_for_config_id(
    args: Configuration, own_app_cfg: configparser.ConfigParser
):
    args.client_id = "my-script-app"
    args.client_secret = ""
    assert resolve_reddit_credentials(args, own_app_cfg) == ("my-script-app", None)


@pytest.mark.parametrize("keyword", ("none", "None", " NONE "))
def test_resolve_credentials_none_keyword_selects_installed_app(
    keyword: str, args: Configuration, own_app_cfg: configparser.ConfigParser
):
    """`--client-secret ""` does not survive Windows PowerShell 5.1, which drops empty arguments."""
    args.client_id = "my-script-app"
    args.client_secret = keyword
    assert resolve_reddit_credentials(args, own_app_cfg) == ("my-script-app", None)


def test_resolve_credentials_accepts_numbers_from_yaml(args: Configuration):
    """A YAML --opts file loads an all-digit value as an int."""
    parser = configparser.ConfigParser()
    parser["DEFAULT"] = {"client_id": "config-app", "client_secret": "config-secret"}
    args.client_id = 1234567
    args.client_secret = 7654321
    assert resolve_reddit_credentials(args, parser) == ("1234567", "7654321")


def test_resolve_credentials_explicit_secret_overrides_config_secret(
    args: Configuration, bundled_cfg: configparser.ConfigParser
):
    args.client_secret = SECRET_SENTINEL
    client_id, client_secret = resolve_reddit_credentials(args, bundled_cfg)
    assert client_id == bundled_cfg.get("DEFAULT", "client_id")
    assert client_secret == SECRET_SENTINEL


@pytest.mark.parametrize("blank_secret", ("", "   ", None))
def test_resolve_credentials_blank_secret_becomes_none(
    blank_secret: str | None, args: Configuration, bundled_cfg: configparser.ConfigParser
):
    """PRAW only uses the installed-app grant when the secret is None."""
    args.client_id = "my-installed-app"
    args.client_secret = blank_secret
    assert resolve_reddit_credentials(args, bundled_cfg) == ("my-installed-app", None)


def test_resolve_credentials_blank_secret_in_config_becomes_none(args: Configuration):
    parser = configparser.ConfigParser()
    parser["DEFAULT"] = {"client_id": "config-installed-app", "client_secret": ""}
    assert resolve_reddit_credentials(args, parser) == ("config-installed-app", None)


def test_resolve_credentials_blank_client_id_argument_falls_back_to_config(
    args: Configuration, bundled_cfg: configparser.ConfigParser
):
    args.client_id = "  "
    client_id, _ = resolve_reddit_credentials(args, bundled_cfg)
    assert client_id == bundled_cfg.get("DEFAULT", "client_id")


def test_resolve_credentials_without_any_client_id_raises(args: Configuration):
    with pytest.raises(BulkDownloaderException, match="client ID"):
        resolve_reddit_credentials(args, configparser.ConfigParser())


def test_user_agent_format_without_username(no_hostname):
    assert build_user_agent() == f"{platform.system().lower()}:bdfr:{__version__}"


def test_user_agent_format_with_username(no_hostname):
    expected = f"{platform.system().lower()}:bdfr:{__version__} (by /u/alice_01)"
    assert build_user_agent("alice_01") == expected


@pytest.mark.parametrize("raw_name", ("u/alice_01", "/u/alice_01/", "  alice_01  ", "U/alice_01"))
def test_user_agent_normalises_username(raw_name: str):
    assert build_user_agent(raw_name).endswith(" (by /u/alice_01)")


@pytest.mark.parametrize("bad_name", ("not a name", "xy", "a" * 21, "bob\r\nX-Evil: 1"))
def test_user_agent_leaves_out_invalid_username(bad_name: str, caplog: pytest.LogCaptureFixture):
    user_agent = build_user_agent(bad_name)
    assert user_agent == build_user_agent()
    assert "out of the User-Agent" in caplog.text


def test_user_agent_never_logs_a_rejected_username(caplog: pytest.LogCaptureFixture):
    """A secret pasted into the username field must not end up in the log or the GUI."""
    pasted_secret = "Xy9-fake-secret-value-abcdef12"
    assert build_user_agent(pasted_secret) == build_user_agent()
    assert "out of the User-Agent" in caplog.text
    assert pasted_secret not in caplog.text
    assert pasted_secret[:8] not in caplog.text


def test_user_agent_accepts_numeric_username_from_yaml(no_hostname):
    """YAML loads `reddit_username: 1234567` as an int; it is still a valid username."""
    assert build_user_agent(1234567).endswith(" (by /u/1234567)")


def test_user_agent_accepts_numeric_override_from_yaml():
    assert build_user_agent(override=42) == "42"


@pytest.mark.parametrize("blank", ("", "   ", None))
def test_user_agent_blank_username_is_omitted(blank: str | None):
    assert "(by /u/" not in build_user_agent(blank)


def test_user_agent_override_is_used_verbatim():
    assert build_user_agent("alice", "  linux:my.tool:v2 (by /u/bob)  ") == "linux:my.tool:v2 (by /u/bob)"


@pytest.mark.parametrize(
    "bad_override",
    (
        "agent\r\nX-Injected: yes",
        "agent\x7f",
        # http.client encodes headers as Latin-1, so this failed the first request inside PRAW.
        "windows:bdfr:2.6.2 (by /u/名前)",
        "windows:bdfr:2.6.2 (by /u/böb)",
    ),
)
def test_user_agent_override_rejects_line_breaks_and_non_ascii(bad_override: str):
    with pytest.raises(BulkDownloaderException, match="single line of plain ASCII"):
        build_user_agent(override=bad_override)


def test_user_agent_never_contains_hostname(no_hostname):
    for user_agent in (build_user_agent(), build_user_agent("alice")):
        assert HOSTNAME_SENTINEL not in user_agent


def test_describe_reddit_client_names_the_bundled_client(bundled_cfg: configparser.ConfigParser):
    bundled_id = bundled_cfg.get("DEFAULT", "client_id")
    description = describe_reddit_client(bundled_id, bundled_cfg.get("DEFAULT", "client_secret"))
    assert "bundled" in description
    assert bundled_cfg.get("DEFAULT", "client_secret") not in description


def test_describe_reddit_client_shows_at_most_four_characters_of_user_id():
    description = describe_reddit_client("abcdefghijklmnop", SECRET_SENTINEL)
    assert "abcd" in description
    assert "abcde" not in description
    assert SECRET_SENTINEL not in description


def test_describe_reddit_client_mentions_installed_app():
    assert "installed app" in describe_reddit_client("abcdefghijklmnop", None)


def test_create_reddit_instance_unauthenticated(
    downloader_mock: MagicMock, bundled_cfg: configparser.ConfigParser, fake_praw_reddit: MagicMock, no_hostname
):
    downloader_mock.cfg_parser = bundled_cfg
    downloader_mock.args.reddit_username = "alice"
    RedditConnector.create_reddit_instance(downloader_mock)

    kwargs = fake_praw_reddit.call_args.kwargs
    assert kwargs["client_id"] == bundled_cfg.get("DEFAULT", "client_id")
    assert kwargs["client_secret"] == bundled_cfg.get("DEFAULT", "client_secret")
    assert kwargs["user_agent"] == f"{platform.system().lower()}:bdfr:{__version__} (by /u/alice)"
    assert kwargs["check_for_updates"] is False
    assert "token_manager" not in kwargs
    assert HOSTNAME_SENTINEL not in kwargs["user_agent"]
    assert downloader_mock.authenticated is False


def test_create_reddit_instance_explicit_credentials_beat_config(
    downloader_mock: MagicMock, bundled_cfg: configparser.ConfigParser, fake_praw_reddit: MagicMock
):
    downloader_mock.cfg_parser = bundled_cfg
    downloader_mock.args.client_id = "my-own-client-id"
    downloader_mock.args.client_secret = ""
    downloader_mock.args.user_agent = "linux:my.tool:v2 (by /u/bob)"
    RedditConnector.create_reddit_instance(downloader_mock)

    kwargs = fake_praw_reddit.call_args.kwargs
    assert kwargs["client_id"] == "my-own-client-id"
    assert kwargs["client_secret"] is None
    assert kwargs["user_agent"] == "linux:my.tool:v2 (by /u/bob)"


def test_create_reddit_instance_authenticated(
    downloader_mock: MagicMock, bundled_cfg: configparser.ConfigParser, fake_praw_reddit: MagicMock, no_hostname
):
    bundled_cfg["DEFAULT"]["user_token"] = "stored-refresh-token"
    bundled_cfg["DEFAULT"]["user_token_client_id"] = "my-own-client-id"
    downloader_mock.cfg_parser = bundled_cfg
    downloader_mock.args.authenticate = True
    downloader_mock.args.client_id = "my-own-client-id"
    downloader_mock.args.client_secret = SECRET_SENTINEL
    RedditConnector.create_reddit_instance(downloader_mock)

    kwargs = fake_praw_reddit.call_args.kwargs
    assert kwargs["client_id"] == "my-own-client-id"
    assert kwargs["client_secret"] == SECRET_SENTINEL
    assert kwargs["check_for_updates"] is False
    assert kwargs["token_manager"] is not None
    assert kwargs["user_agent"] == f"{platform.system().lower()}:bdfr:{__version__}"
    assert downloader_mock.authenticated is True


@pytest.mark.parametrize("authenticate", (False, True))
def test_create_reddit_instance_never_logs_the_secret(
    authenticate: bool,
    downloader_mock: MagicMock,
    bundled_cfg: configparser.ConfigParser,
    fake_praw_reddit: MagicMock,
    caplog: pytest.LogCaptureFixture,
):
    caplog.set_level(1)
    bundled_cfg["DEFAULT"]["user_token"] = "stored-refresh-token"
    bundled_cfg["DEFAULT"]["user_token_client_id"] = "abcdefghijklmnop"
    downloader_mock.cfg_parser = bundled_cfg
    downloader_mock.args.authenticate = authenticate
    downloader_mock.args.client_id = "abcdefghijklmnop"
    downloader_mock.args.client_secret = SECRET_SENTINEL
    RedditConnector.create_reddit_instance(downloader_mock)

    assert caplog.records, "expected the credential source to be logged"
    assert SECRET_SENTINEL not in caplog.text
    assert "abcdefghijklmnop" not in caplog.text
    assert "user-supplied" in caplog.text


def test_create_reddit_instance_logs_bundled_client_without_secret(
    downloader_mock: MagicMock,
    bundled_cfg: configparser.ConfigParser,
    fake_praw_reddit: MagicMock,
    caplog: pytest.LogCaptureFixture,
):
    caplog.set_level(1)
    downloader_mock.cfg_parser = bundled_cfg
    RedditConnector.create_reddit_instance(downloader_mock)
    assert "bundled" in caplog.text
    assert bundled_cfg.get("DEFAULT", "client_secret") not in caplog.text


def test_create_reddit_instance_oauth_flow_gets_resolved_credentials(
    downloader_mock: MagicMock,
    bundled_cfg: configparser.ConfigParser,
    fake_praw_reddit: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
):
    """A first-time login must authorise the user's own app, not the bundled one."""
    authenticator = MagicMock()
    authenticator.return_value.retrieve_new_token.return_value = "new-refresh-token"
    monkeypatch.setattr("bdfr.connector.OAuth2Authenticator", authenticator)
    authenticator.split_scopes.return_value = {"read"}
    downloader_mock.cfg_parser = bundled_cfg
    downloader_mock.args.authenticate = True
    downloader_mock.args.client_id = "my-own-client-id"
    downloader_mock.args.client_secret = ""
    downloader_mock.args.reddit_username = "alice"
    RedditConnector.create_reddit_instance(downloader_mock)

    assert authenticator.call_args.args[1:] == ("my-own-client-id", None)
    assert authenticator.call_args.kwargs["user_agent"] == fake_praw_reddit.call_args.kwargs["user_agent"]
    assert authenticator.call_args.kwargs["user_agent"].endswith("(by /u/alice)")
    assert bundled_cfg.get("DEFAULT", "user_token") == "new-refresh-token"
    # Recorded so that a later run with another app is refused with a clear message.
    assert bundled_cfg.get("DEFAULT", "user_token_client_id") == "my-own-client-id"
    downloader_mock.write_config_if_changed.assert_called_once_with()


def _authenticated_run(downloader_mock: MagicMock, cfg: configparser.ConfigParser, client_id: str | None):
    downloader_mock.cfg_parser = cfg
    downloader_mock.config_location = Path("C:/fake/config.cfg")
    downloader_mock.args.authenticate = True
    downloader_mock.args.client_id = client_id
    downloader_mock.args.client_secret = SECRET_SENTINEL if client_id else None
    RedditConnector.create_reddit_instance(downloader_mock)


@pytest.mark.parametrize(
    ("recorded_issuer", "client_id"),
    (
        # Token from the user's own app, run falls back to the bundled one.
        ("abcdefghijklmnop", None),
        # Token from one app of the user's, run with another.
        ("abcdefghijklmnop", "zyxwvutsrqponm"),
        # A token saved before issuers were recorded came from config.cfg's client_id.
        (None, "zyxwvutsrqponm"),
    ),
)
def test_create_reddit_instance_refuses_token_from_another_client(
    recorded_issuer: str | None,
    client_id: str | None,
    downloader_mock: MagicMock,
    bundled_cfg: configparser.ConfigParser,
    fake_praw_reddit: MagicMock,
):
    bundled_cfg["DEFAULT"]["user_token"] = "stored-refresh-token"
    if recorded_issuer:
        bundled_cfg["DEFAULT"]["user_token_client_id"] = recorded_issuer

    with pytest.raises(RedditAuthenticationError) as caught:
        _authenticated_run(downloader_mock, bundled_cfg, client_id)

    fake_praw_reddit.assert_not_called()
    message = str(caught.value)
    assert "saved Reddit login" in message
    assert "user_token" in message
    assert "config.cfg" in message
    if client_id is None:
        # The bundled app: the user has no ID or secret of it to pass, and needs none.
        assert "without --client-id" in message
        assert "--client-secret" not in message
    else:
        assert "this app's --client-id and --client-secret" in message
        assert "http://localhost:7634" in message
    for secret_or_full_id in (SECRET_SENTINEL, "abcdefghijklmnop", "zyxwvutsrqponm"):
        assert secret_or_full_id not in message
    # The stored token is left alone: the user may simply switch back.
    assert bundled_cfg.get("DEFAULT", "user_token") == "stored-refresh-token"


@pytest.mark.parametrize(
    ("recorded_issuer", "client_id"),
    (
        ("abcdefghijklmnop", "abcdefghijklmnop"),
        # A legacy token and a run with the app config.cfg holds: the case before this option existed.
        (None, None),
        (None, "U-6gk4ZCh3IeNQ"),
    ),
)
def test_create_reddit_instance_accepts_token_from_the_same_client(
    recorded_issuer: str | None,
    client_id: str | None,
    downloader_mock: MagicMock,
    bundled_cfg: configparser.ConfigParser,
    fake_praw_reddit: MagicMock,
):
    bundled_cfg["DEFAULT"]["user_token"] = "stored-refresh-token"
    if recorded_issuer:
        bundled_cfg["DEFAULT"]["user_token_client_id"] = recorded_issuer
    _authenticated_run(downloader_mock, bundled_cfg, client_id)
    assert fake_praw_reddit.call_args.kwargs["token_manager"] is not None


USER_CLIENT_ID = "abcdefghijklmnop"

# How Reddit answers the token request of an app whose credentials it refuses.
CREDENTIAL_REJECTIONS = (
    # A script or web app's ID sent without its secret, or with a wrong one.
    pytest.param(401, {"message": "Unauthorized", "error": 401}, id="http-401"),
    # Some OAuth failures come back inside a 200 response.
    pytest.param(200, {"error": "unauthorized_client"}, id="oauth-error"),
)


class _FakeResponse:
    """Just enough of requests.Response for prawcore's token request."""

    def __init__(self, status_code: int, payload: dict, headers: dict | None = None):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = json.dumps(payload)
        self._payload = payload

    def json(self) -> dict:
        return self._payload


@pytest.fixture()
def fake_reddit_http(monkeypatch: pytest.MonkeyPatch):
    """Answer every HTTP request PRAW makes with one canned response, so nothing reaches Reddit."""

    def install(status_code: int, payload: dict) -> list[str]:
        requested_urls = []

        def fake_request(_session, _method, url, *_args, **_kwargs):
            requested_urls.append(url)
            return _FakeResponse(status_code, payload)

        monkeypatch.setattr(requests.Session, "request", fake_request)
        return requested_urls

    return install


@pytest.fixture()
def fake_sleep(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    fake = MagicMock()
    monkeypatch.setattr("bdfr.connector.sleep", fake)
    return fake


def _real_reddit_run(
    downloader_mock: MagicMock,
    cfg: configparser.ConfigParser,
    client_secret: str | None,
    config_location: Path,
    authenticate: bool = False,
):
    """Build a real PRAW instance with the user's own app, as a GUI job does."""
    downloader_mock.cfg_parser = cfg
    downloader_mock.config_location = config_location
    downloader_mock.args.authenticate = authenticate
    downloader_mock.args.client_id = USER_CLIENT_ID
    downloader_mock.args.client_secret = client_secret
    RedditConnector.create_reddit_instance(downloader_mock)
    downloader_mock.check_user_existence = lambda name: RedditConnector.check_user_existence(downloader_mock, name)
    downloader_mock.check_subreddit_status = RedditConnector.check_subreddit_status


def _assert_nothing_leaked(message: str, log_text: str):
    for leaked in (SECRET_SENTINEL, USER_CLIENT_ID, "stored-refresh-token"):
        assert leaked not in message
        assert leaked not in log_text


@pytest.mark.parametrize("client_secret", (None, SECRET_SENTINEL), ids=("secret-left-out", "wrong-secret"))
@pytest.mark.parametrize(("status_code", "payload"), CREDENTIAL_REJECTIONS)
def test_rejected_credentials_stop_a_user_run_at_the_first_request(
    client_secret: str | None,
    status_code: int,
    payload: dict,
    downloader_mock: MagicMock,
    bundled_cfg: configparser.ConfigParser,
    fake_reddit_http,
    fake_sleep: MagicMock,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
):
    """Each user used to be logged as failed, a minute apart, and the GUI job then showed "completed"."""
    caplog.set_level(1)
    requested_urls = fake_reddit_http(status_code, payload)
    _real_reddit_run(downloader_mock, bundled_cfg, client_secret, tmp_path / "config.cfg")
    downloader_mock.args.user = ["alice", "bob"]
    downloader_mock.args.submitted = True

    with pytest.raises(RedditAuthenticationError) as caught:
        RedditConnector.get_user_data(downloader_mock)

    fake_sleep.assert_not_called()
    # Only the token request was made: bob is not tried with credentials already refused.
    assert len(requested_urls) == 1
    assert requested_urls[0].endswith("/api/v1/access_token")
    message = str(caught.value)
    assert message.startswith("Reddit rejected the API credentials of a user-supplied client ID starting 'abcd'")
    assert "a script or web app needs its secret, and an installed app has none" in message
    assert "failed to be retrieved" not in caplog.text
    _assert_nothing_leaked(message, caplog.text)


@pytest.mark.parametrize(("status_code", "payload"), CREDENTIAL_REJECTIONS)
def test_rejected_credentials_stop_a_subreddit_run(
    status_code: int,
    payload: dict,
    downloader_mock: MagicMock,
    bundled_cfg: configparser.ConfigParser,
    fake_reddit_http,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
):
    """This used to escape as a bare "ResponseException: received 401 HTTP response"."""
    caplog.set_level(1)
    requested_urls = fake_reddit_http(status_code, payload)
    _real_reddit_run(downloader_mock, bundled_cfg, None, tmp_path / "config.cfg")
    downloader_mock.args.subreddit = ["EarthPorn", "aww"]

    with pytest.raises(RedditAuthenticationError, match="Reddit rejected the API credentials") as caught:
        RedditConnector.get_subreddits(downloader_mock)

    assert len(requested_urls) == 1
    _assert_nothing_leaked(str(caught.value), caplog.text)


def test_rejected_credentials_of_an_authenticated_run_mention_the_saved_login(
    downloader_mock: MagicMock,
    bundled_cfg: configparser.ConfigParser,
    fake_reddit_http,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
):
    caplog.set_level(1)
    fake_reddit_http(401, {"message": "Unauthorized", "error": 401})
    bundled_cfg["DEFAULT"]["user_token"] = "stored-refresh-token"
    bundled_cfg["DEFAULT"]["user_token_client_id"] = USER_CLIENT_ID
    _real_reddit_run(downloader_mock, bundled_cfg, SECRET_SENTINEL, tmp_path / "config.cfg", authenticate=True)

    with pytest.raises(RedditAuthenticationError) as caught:
        RedditConnector.resolve_user_name(downloader_mock, "me")

    message = str(caught.value)
    assert "the saved login may have been revoked" in message
    assert "user_token" in message
    _assert_nothing_leaked(message, caplog.text)


def test_reddit_outage_during_setup_is_waited_out_then_logged_per_user(
    downloader_mock: MagicMock,
    bundled_cfg: configparser.ConfigParser,
    fake_reddit_http,
    fake_sleep: MagicMock,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
):
    """Only refused credentials end the run. An outage is retried with a growing backoff, a bounded
    number of times, before the user is reported and the run carries on with the next one."""
    requested_urls = fake_reddit_http(500, {})
    _real_reddit_run(downloader_mock, bundled_cfg, SECRET_SENTINEL, tmp_path / "config.cfg")
    downloader_mock.args.user = ["alice"]
    downloader_mock.args.submitted = True

    assert RedditConnector.get_user_data(downloader_mock) == []
    assert fake_sleep.call_args_list == [call(15), call(30), call(60), call(120)]
    assert len(requested_urls) == MAX_ATTEMPTS
    assert "User alice failed to be retrieved" in caplog.text


TOKEN_PATH = "/api/v1/access_token"
TOKEN_GRANTED = {"access_token": "token", "expires_in": 3600, "scope": "*", "token_type": "bearer"}
RATE_LIMITED = {"message": "Too Many Requests", "error": 429}
USER_FOUND = {"kind": "t2", "data": {"name": "alice", "id": "abc12"}}


@pytest.fixture()
def scripted_reddit_http(monkeypatch: pytest.MonkeyPatch):
    """Answer each path from its own queue of responses, and grant any token request left unscripted."""

    def install(script: dict[str, list[_FakeResponse]]) -> list[str]:
        requested_paths = []

        def fake_request(_session, _method, url, *_args, **_kwargs):
            path = urlparse(url).path.rstrip("/")
            requested_paths.append(path)
            if path == TOKEN_PATH and not script.get(path):
                return _FakeResponse(200, TOKEN_GRANTED)
            return script[path].pop(0)

        monkeypatch.setattr(requests.Session, "request", fake_request)
        return requested_paths

    return install


def test_rate_limit_during_setup_no_longer_drops_the_user(
    downloader_mock: MagicMock,
    bundled_cfg: configparser.ConfigParser,
    scripted_reddit_http,
    fake_sleep: MagicMock,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
):
    """A 429 on the existence check used to cost the user all of their posts after a one-minute pause."""
    requested_paths = scripted_reddit_http(
        {
            "/user/alice/about": [
                # No x-ratelimit-remaining, so prawcore's own limiter does not sleep for real.
                _FakeResponse(429, RATE_LIMITED, {"x-ratelimit-reset": "30"}),
                _FakeResponse(200, USER_FOUND),
            ]
        }
    )
    _real_reddit_run(downloader_mock, bundled_cfg, SECRET_SENTINEL, tmp_path / "config.cfg")
    downloader_mock.args.user = ["alice"]
    downloader_mock.args.submitted = True

    result = RedditConnector.get_user_data(downloader_mock)

    assert [listing.description for listing in result] == ["submitted posts of u/alice"]
    fake_sleep.assert_called_once_with(30 + RATE_LIMIT_MARGIN)
    assert requested_paths.count("/user/alice/about") == 2
    assert "rate limit" in caplog.text
    assert "failed to be retrieved" not in caplog.text


def test_rate_limit_during_setup_no_longer_drops_the_subreddit(
    downloader_mock: MagicMock,
    bundled_cfg: configparser.ConfigParser,
    scripted_reddit_http,
    fake_sleep: MagicMock,
    tmp_path: Path,
):
    """It used to escape get_subreddits and end the whole run."""
    scripted_reddit_http(
        {
            "/r/EarthPorn/about": [
                _FakeResponse(429, RATE_LIMITED),
                _FakeResponse(200, {"kind": "t5", "data": {"display_name": "EarthPorn", "id": "2sbq3"}}),
            ]
        }
    )
    _real_reddit_run(downloader_mock, bundled_cfg, SECRET_SENTINEL, tmp_path / "config.cfg")
    downloader_mock.args.subreddit = ["EarthPorn"]

    result = RedditConnector.get_subreddits(downloader_mock)

    assert [listing.description for listing in result] == ["posts of r/EarthPorn"]
    fake_sleep.assert_called_once_with(60)


def test_rate_limited_token_request_during_setup_no_longer_drops_the_user(
    downloader_mock: MagicMock,
    bundled_cfg: configparser.ConfigParser,
    scripted_reddit_http,
    fake_sleep: MagicMock,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
):
    """A GUI job is a fresh process whose first request asks for a token. prawcore raises that endpoint's
    429 as a bare ResponseException, which used to drop the job's only user at once, without any wait."""
    requested_paths = scripted_reddit_http(
        {
            TOKEN_PATH: [_FakeResponse(429, RATE_LIMITED)],
            "/user/alice/about": [_FakeResponse(200, USER_FOUND)],
        }
    )
    _real_reddit_run(downloader_mock, bundled_cfg, SECRET_SENTINEL, tmp_path / "config.cfg")
    downloader_mock.args.user = ["alice"]
    downloader_mock.args.submitted = True

    result = RedditConnector.get_user_data(downloader_mock)

    assert [listing.description for listing in result] == ["submitted posts of u/alice"]
    # The token endpoint's 429 says nothing about when the limit resets.
    fake_sleep.assert_called_once_with(DEFAULT_RATE_LIMIT_WAIT)
    assert requested_paths == [TOKEN_PATH, TOKEN_PATH, "/user/alice/about"]
    assert "rate limit" in caplog.text
    assert "failed to be retrieved" not in caplog.text


def test_token_rate_limit_that_outlasts_the_retries_drops_only_that_user(
    downloader_mock: MagicMock,
    bundled_cfg: configparser.ConfigParser,
    scripted_reddit_http,
    fake_sleep: MagicMock,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
):
    """Users used to be dropped back to back within a second; now each attempt waits for the window to reset."""
    scripted_reddit_http(
        {
            TOKEN_PATH: [_FakeResponse(429, RATE_LIMITED) for _ in range(MAX_ATTEMPTS)],
            "/user/bob/about": [_FakeResponse(200, {"kind": "t2", "data": {"name": "bob", "id": "def34"}})],
        }
    )
    _real_reddit_run(downloader_mock, bundled_cfg, SECRET_SENTINEL, tmp_path / "config.cfg")
    downloader_mock.args.user = ["alice", "bob"]
    downloader_mock.args.submitted = True

    result = RedditConnector.get_user_data(downloader_mock)

    assert [listing.description for listing in result] == ["submitted posts of u/bob"]
    assert fake_sleep.call_args_list == [call(DEFAULT_RATE_LIMIT_WAIT)] * (MAX_ATTEMPTS - 1)
    assert "User alice failed to be retrieved" in caplog.text


def test_subreddit_whose_check_outlasts_the_retries_is_dropped_not_the_run(
    downloader_mock: MagicMock,
    bundled_cfg: configparser.ConfigParser,
    scripted_reddit_http,
    fake_sleep: MagicMock,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
):
    """The last error used to escape get_subreddits and end the run, so the other sources were never tried."""
    scripted_reddit_http(
        {
            "/r/EarthPorn/about": [
                _FakeResponse(429, RATE_LIMITED, {"x-ratelimit-reset": "30"}) for _ in range(MAX_ATTEMPTS)
            ],
            "/r/aww/about": [_FakeResponse(200, {"kind": "t5", "data": {"display_name": "aww", "id": "2qh1o"}})],
        }
    )
    _real_reddit_run(downloader_mock, bundled_cfg, SECRET_SENTINEL, tmp_path / "config.cfg")
    downloader_mock.args.subreddit = ["EarthPorn", "aww"]

    result = RedditConnector.get_subreddits(downloader_mock)

    assert [listing.description for listing in result] == ["posts of r/aww"]
    assert fake_sleep.call_args_list == [call(30 + RATE_LIMIT_MARGIN)] * (MAX_ATTEMPTS - 1)
    assert "Failed to get submissions for subreddit EarthPorn" in caplog.text


@pytest.fixture()
def offline_reddit() -> praw.Reddit:
    """A real PRAW instance for building listings; building one never makes a request."""
    return praw.Reddit(
        client_id="test-client-id",
        client_secret="test-client-secret",
        user_agent="test:bdfr:connector",
        check_for_updates=False,
    )


def _resume(listing: ResumableListing) -> praw.models.ListingGenerator:
    """Build the PRAW generator a listing resumes with once post abc has been handed out."""
    generator = listing.factory(limit=7, params={"after": "t3_abc"})
    assert isinstance(generator, praw.models.ListingGenerator)
    assert generator.params["after"] == "t3_abc"
    assert generator.limit == 7
    return generator


def test_user_listings_are_resumable(downloader_mock: MagicMock, offline_reddit: praw.Reddit):
    downloader_mock.reddit_instance = offline_reddit
    downloader_mock.authenticated = True
    downloader_mock.args.user = ["alice"]
    downloader_mock.args.submitted = downloader_mock.args.upvoted = downloader_mock.args.saved = True
    downloader_mock.args.limit = 25
    downloader_mock.args.sort = "top"
    downloader_mock.args.time = "week"
    downloader_mock.sort_filter = RedditConnector.create_sort_filter(downloader_mock)
    downloader_mock.time_filter = RedditConnector.create_time_filter(downloader_mock)
    downloader_mock.determine_sort_function.return_value = RedditConnector.determine_sort_function(downloader_mock)

    listings = RedditConnector.get_user_data(downloader_mock)

    assert all(isinstance(listing, ResumableListing) for listing in listings)
    assert [listing.description for listing in listings] == [
        "submitted posts of u/alice",
        "upvoted posts of u/alice",
        "saved posts of u/alice",
    ]
    assert all(listing.limit == 25 for listing in listings)
    submitted, upvoted, saved = (_resume(listing) for listing in listings)
    assert submitted.url.rstrip("/").endswith("user/alice/submitted")
    assert (submitted.params["sort"], submitted.params["t"]) == ("top", "week")
    assert upvoted.url.endswith("user/alice/upvoted")
    assert saved.url.endswith("user/alice/saved")


def test_subreddit_listing_is_resumable(downloader_mock: MagicMock, offline_reddit: praw.Reddit):
    downloader_mock.reddit_instance = offline_reddit
    downloader_mock.args.subreddit = ["EarthPorn"]
    downloader_mock.args.limit = 10
    downloader_mock.sort_filter = RedditTypes.SortType.NEW
    downloader_mock.determine_sort_function.return_value = RedditConnector.determine_sort_function(downloader_mock)

    [listing] = RedditConnector.get_subreddits(downloader_mock)

    assert listing.description == "posts of r/EarthPorn"
    assert listing.limit == 10
    assert _resume(listing).url == "r/EarthPorn/new"


def test_subreddit_search_is_resumable(downloader_mock: MagicMock, offline_reddit: praw.Reddit):
    downloader_mock.reddit_instance = offline_reddit
    downloader_mock.args.subreddit = ["EarthPorn"]
    downloader_mock.args.search = "sunset"
    downloader_mock.sort_filter = RedditTypes.SortType.TOP
    downloader_mock.time_filter = RedditTypes.TimeType.YEAR

    [listing] = RedditConnector.get_subreddits(downloader_mock)

    assert listing.description == 'posts in r/EarthPorn matching "sunset"'
    params = _resume(listing).params
    assert (params["q"], params["sort"], params["t"], params["restrict_sr"]) == ("sunset", "top", "year", True)


def test_archiver_comment_listing_is_resumable(offline_reddit: praw.Reddit):
    # __init__ would read the config and contact Reddit; get_user_data needs none of that.
    archiver = Archiver.__new__(Archiver)
    archiver.args = Configuration()
    archiver.args.user = ["alice"]
    archiver.args.all_comments = True
    archiver.args.limit = 30
    archiver.reddit_instance = offline_reddit
    archiver.authenticated = False
    archiver.sort_filter = RedditTypes.SortType.NEW

    [listing] = archiver.get_user_data()

    assert isinstance(listing, ResumableListing)
    assert listing.description == "comments of u/alice"
    assert listing.limit == 30
    comments = _resume(listing)
    assert comments.url.rstrip("/").endswith("user/alice/comments")
    assert comments.params["sort"] == "new"


def test_rejected_bundled_app_suggests_registering_an_own_app(bundled_cfg: configparser.ConfigParser):
    reddit = MagicMock()
    reddit.config.client_id = bundled_cfg.get("DEFAULT", "client_id")
    reddit.config.client_secret = bundled_cfg.get("DEFAULT", "client_secret")
    reddit.read_only = True
    error = prawcore.ResponseException(MagicMock(status_code=401))

    with pytest.raises(RedditAuthenticationError) as caught:
        raise_if_credentials_rejected(error, reddit)

    message = str(caught.value)
    assert "bundled with BDFR" in message
    assert "register your own app" in message
    assert "user_token" not in message
    assert bundled_cfg.get("DEFAULT", "client_secret") not in message
    assert caught.value.__cause__ is error


@pytest.mark.parametrize(
    "error",
    (
        prawcore.NotFound(MagicMock(status_code=404)),
        prawcore.Forbidden(MagicMock(status_code=403)),
        prawcore.ServerError(MagicMock(status_code=500)),
        prawcore.TooManyRequests(MagicMock(status_code=429, headers={})),
        prawcore.RequestException(OSError("connection reset"), (), {}),
        BulkDownloaderException("Could not find user alice"),
    ),
)
def test_raise_if_credentials_rejected_ignores_other_errors(error: Exception):
    raise_if_credentials_rejected(error, MagicMock())


def test_user_dropped_at_setup_is_a_run_summary(downloader_mock: MagicMock, caplog: pytest.LogCaptureFixture):
    """A user that cannot be used must reach a GUI job's final message, not just the log.

    Otherwise the job ends as "Finished" with nothing downloaded and no reason.
    """
    from bdfr.jobs import _QueueLogHandler

    downloader_mock.args.user = ["ghost"]
    downloader_mock.args.submitted = True
    downloader_mock.check_user_existence.side_effect = BulkDownloaderException("Could not find user ghost")
    handler = _QueueLogHandler("job", MagicMock())
    logging.getLogger().addHandler(handler)
    try:
        with caplog.at_level(logging.ERROR):
            generators = RedditConnector.get_user_data(downloader_mock)
    finally:
        logging.getLogger().removeHandler(handler)

    assert generators == []
    assert "Could not find user ghost" in handler.completion_message
    assert any(getattr(r, "bdfr_event", None) == "run_summary" for r in caplog.records)
