#!/usr/bin/env python3

import configparser
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from bdfr.connector import build_user_agent
from bdfr.exceptions import BulkDownloaderException
from bdfr.oauth2 import OAuth2Authenticator, OAuth2TokenManager


@pytest.fixture()
def example_config() -> configparser.ConfigParser:
    out = configparser.ConfigParser()
    config_dict = {"DEFAULT": {"user_token": "example"}}
    out.read_dict(config_dict)
    return out


@pytest.mark.online
@pytest.mark.parametrize(
    "test_scopes",
    (
        {
            "history",
        },
        {"history", "creddits"},
        {"account", "flair"},
        {
            "*",
        },
    ),
)
def test_check_scopes(test_scopes: set[str]):
    OAuth2Authenticator._check_scopes(test_scopes, build_user_agent())


@pytest.mark.parametrize(
    ("test_scopes", "expected"),
    (
        (
            "history",
            {
                "history",
            },
        ),
        ("history creddits", {"history", "creddits"}),
        ("history, creddits, account", {"history", "creddits", "account"}),
        ("history,creddits,account,flair", {"history", "creddits", "account", "flair"}),
    ),
)
def test_split_scopes(test_scopes: str, expected: set[str]):
    result = OAuth2Authenticator.split_scopes(test_scopes)
    assert result == expected


@pytest.mark.online
@pytest.mark.parametrize(
    "test_scopes",
    (
        {
            "random",
        },
        {"scope", "another_scope"},
    ),
)
def test_check_scopes_bad(test_scopes: set[str]):
    with pytest.raises(BulkDownloaderException):
        OAuth2Authenticator._check_scopes(test_scopes, build_user_agent())


def test_login_sends_the_given_user_agent_and_skips_update_check(monkeypatch: pytest.MonkeyPatch):
    """The one-time login must be as compliant as the run itself, and must not contact PyPI."""
    user_agent = "windows:bdfr:9.9.9 (by /u/alice)"
    fake_get = MagicMock()
    fake_get.return_value.json.return_value = {"read": {}}
    monkeypatch.setattr("bdfr.oauth2.requests.get", fake_get)
    fake_reddit = MagicMock()
    fake_reddit.return_value.auth.url.return_value = "https://www.reddit.com/api/v1/authorize"
    fake_reddit.return_value.auth.authorize.return_value = "new-refresh-token"
    monkeypatch.setattr("bdfr.oauth2.praw.Reddit", fake_reddit)
    monkeypatch.setattr("bdfr.oauth2.random.randint", lambda *_: 1234)
    client = MagicMock()
    client.recv.return_value = b"GET /?state=1234&code=abc HTTP/1.1"
    monkeypatch.setattr(OAuth2Authenticator, "receive_connection", staticmethod(lambda: client))

    authenticator = OAuth2Authenticator({"read"}, "my-client-id", None, user_agent=user_agent)
    assert authenticator.retrieve_new_token() == "new-refresh-token"

    assert fake_get.call_args.kwargs["headers"] == {"User-Agent": user_agent}
    kwargs = fake_reddit.call_args.kwargs
    assert kwargs["user_agent"] == user_agent
    assert kwargs["check_for_updates"] is False
    assert kwargs["redirect_uri"] == "http://localhost:7634"
    assert (kwargs["client_id"], kwargs["client_secret"]) == ("my-client-id", None)


def test_token_manager_read(example_config: configparser.ConfigParser):
    mock_authoriser = MagicMock()
    mock_authoriser.refresh_token = None
    test_manager = OAuth2TokenManager(example_config, MagicMock())
    test_manager.pre_refresh_callback(mock_authoriser)
    assert mock_authoriser.refresh_token == example_config.get("DEFAULT", "user_token")


def test_token_manager_write(example_config: configparser.ConfigParser, tmp_path: Path):
    test_path = tmp_path / "test.cfg"
    mock_authoriser = MagicMock()
    mock_authoriser.refresh_token = "changed_token"
    test_manager = OAuth2TokenManager(example_config, test_path)
    test_manager.post_refresh_callback(mock_authoriser)
    assert example_config.get("DEFAULT", "user_token") == "changed_token"
    with test_path.open("r") as file:
        file_contents = file.read()
    assert "user_token = changed_token" in file_contents
