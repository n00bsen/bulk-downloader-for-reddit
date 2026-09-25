#!/usr/bin/env python3

import configparser
from pathlib import Path

import praw
import pytest

from bdfr.connector import build_user_agent
from bdfr.oauth2 import OAuth2TokenManager


@pytest.fixture(scope="session")
def reddit_instance():
    rd = praw.Reddit(
        client_id="U-6gk4ZCh3IeNQ",
        client_secret="7CZHY6AmKweZME5s50SfDGylaPg",
        user_agent=build_user_agent(),
        check_for_updates=False,
    )
    return rd


@pytest.fixture(scope="session")
def authenticated_reddit_instance():
    test_config_path = Path("./tests/test_config.cfg")
    if not test_config_path.exists():
        pytest.skip("Refresh token must be provided to authenticate with OAuth2")
    cfg_parser = configparser.ConfigParser()
    cfg_parser.read(test_config_path)
    if not cfg_parser.has_option("DEFAULT", "user_token"):
        pytest.skip("Refresh token must be provided to authenticate with OAuth2")
    token_manager = OAuth2TokenManager(cfg_parser, test_config_path)
    reddit_instance = praw.Reddit(
        client_id=cfg_parser.get("DEFAULT", "client_id"),
        client_secret=cfg_parser.get("DEFAULT", "client_secret"),
        # Never the hostname: this is sent to Reddit.
        user_agent=build_user_agent(),
        check_for_updates=False,
        token_manager=token_manager,
    )
    return reddit_instance
