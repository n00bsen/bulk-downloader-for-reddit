#!/usr/bin/env python3

import configparser
from pathlib import Path
from unittest.mock import MagicMock

import click
import pytest

from bdfr.__main__ import cli_download
from bdfr.configuration import Configuration
from bdfr.connector import INSTALLED_APP_SECRET, build_user_agent, resolve_reddit_credentials


@pytest.mark.parametrize(
    "arg_dict",
    (
        {"directory": "test_dir"},
        {
            "directory": "test_dir",
            "no_dupes": True,
        },
    ),
)
def test_process_click_context(arg_dict: dict):
    test_config = Configuration()
    test_context = MagicMock()
    test_context.params = arg_dict
    test_config.process_click_arguments(test_context)
    test_config = vars(test_config)
    assert all(test_config[arg] == arg_dict[arg] for arg in arg_dict.keys())


def test_yaml_file_read():
    file = "./tests/yaml_test_configuration.yaml"
    test_config = Configuration()
    test_config.parse_yaml_options(file)
    assert test_config.subreddit == ["EarthPorn", "TwoXChromosomes", "Mindustry"]
    assert test_config.sort == "new"
    assert test_config.limit == 10


def test_reddit_credentials_default_to_unset():
    """None means "use the config file", which is what keeps existing setups working."""
    test_config = Configuration()
    assert test_config.client_id is None
    assert test_config.client_secret is None
    assert test_config.user_agent is None
    assert test_config.reddit_username is None


def test_cli_credential_options_reach_the_configuration():
    context = cli_download.make_context(
        "download",
        [
            "some_dir",
            "--client-id",
            "my-client-id",
            "--client-secret",
            "my-secret",
            "--user-agent",
            "windows:test:1.0",
            "--reddit-username",
            "alice",
        ],
    )
    test_config = Configuration()
    test_config.process_click_arguments(context)
    assert test_config.client_id == "my-client-id"
    assert test_config.client_secret == "my-secret"
    assert test_config.user_agent == "windows:test:1.0"
    assert test_config.reddit_username == "alice"


def test_cli_empty_client_secret_is_kept():
    """An explicit empty secret selects an installed app, so it must not be dropped as "unset"."""
    context = cli_download.make_context("download", ["some_dir", "--client-id", "my-client-id", "--client-secret", ""])
    test_config = Configuration()
    test_config.process_click_arguments(context)
    assert test_config.client_secret == ""


def test_cli_none_client_secret_selects_installed_app():
    """The PowerShell-safe spelling: Windows PowerShell 5.1 silently drops `""`."""
    context = cli_download.make_context(
        "download", ["some_dir", "--client-id", "my-installed-app", "--client-secret", "none"]
    )
    test_config = Configuration()
    test_config.process_click_arguments(context)
    assert resolve_reddit_credentials(test_config, configparser.ConfigParser()) == ("my-installed-app", None)


@pytest.mark.parametrize("swallowed_option", ("--submitted", "--user", "-u", "--authenticate"))
def test_cli_rejects_an_option_taken_as_the_client_secret(swallowed_option: str):
    """PowerShell 5.1 turns `--client-secret "" --submitted` into `--client-secret --submitted`."""
    with pytest.raises(click.BadParameter, match=f"--client-secret {INSTALLED_APP_SECRET}"):
        cli_download.make_context(
            "download",
            ["some_dir", "--client-id", "my-app", "--client-secret", swallowed_option, "--limit", "5"],
        )


def test_cli_accepts_a_secret_that_starts_with_a_dash():
    """Reddit secrets may start with "-"; only an exact option name is refused."""
    context = cli_download.make_context(
        "download", ["some_dir", "--client-id", "my-app", "--client-secret", "--Xy9-fake-secret-value-abcd"]
    )
    test_config = Configuration()
    test_config.process_click_arguments(context)
    assert test_config.client_secret == "--Xy9-fake-secret-value-abcd"


def test_cli_credential_options_are_optional():
    context = cli_download.make_context("download", ["some_dir"])
    test_config = Configuration()
    test_config.process_click_arguments(context)
    assert test_config.client_id is None
    assert test_config.client_secret is None


def test_cli_help_explains_secret_fallback_and_redirect_uri():
    helps = {param.name: param.help for param in cli_download.params if isinstance(param, click.Option)}
    assert "http://localhost:7634" in helps["client_id"]
    assert "installed" in helps["client_secret"]
    assert "config file" in helps["client_secret"]
    assert f"pass {INSTALLED_APP_SECRET} to force installed-app mode" in helps["client_secret"]
    assert '""' not in helps["client_secret"]


def test_numeric_yaml_credentials_do_not_crash_the_connector(tmp_path: Path):
    """YAML loads all-digit values as ints, which used to raise AttributeError on .strip()."""
    opts = tmp_path / "opts.yaml"
    opts.write_text("client_id: 1234567\nclient_secret: 7654321\nreddit_username: 1234567\n", encoding="utf-8")
    test_config = Configuration()
    test_config.parse_yaml_options(str(opts))
    assert resolve_reddit_credentials(test_config, configparser.ConfigParser()) == ("1234567", "7654321")
    assert build_user_agent(test_config.reddit_username, test_config.user_agent).endswith(" (by /u/1234567)")


def test_repr_masks_client_secret():
    test_config = Configuration()
    test_config.client_id = "visible-client-id"
    test_config.client_secret = "sentinel-secret-value"
    shown = repr(test_config)
    assert "sentinel-secret-value" not in shown
    assert "visible-client-id" in shown
    assert test_config.client_secret == "sentinel-secret-value"
