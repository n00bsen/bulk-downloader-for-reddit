#!/usr/bin/env python3

import logging
import sys

import click
import requests

from bdfr import __version__
from bdfr.archiver import Archiver
from bdfr.cloner import RedditCloner
from bdfr.completion import Completion
from bdfr.configuration import Configuration
from bdfr.connector import INSTALLED_APP_SECRET
from bdfr.constants import OAUTH_REDIRECT_URI, REQUEST_TIMEOUT
from bdfr.downloader import RedditDownloader

logger = logging.getLogger()


def _check_client_secret(context: click.Context, _param: click.Parameter, value: str | None) -> str | None:
    """Refuse another option's name given as the client secret.

    Windows PowerShell 5.1 silently drops an empty argument, so there
    `--client-secret "" --submitted` takes --submitted as the secret. The run
    would then use a bogus secret and, with that option lost, could finish
    having downloaded nothing and without saying why.
    """
    if value is None:
        return value
    option_names = {name for param in context.command.params for name in (*param.opts, *param.secondary_opts)}
    if value.strip() in option_names:
        raise click.BadParameter(
            f"got the option {value.strip()} instead of a secret. For an installed app, which has no secret, "
            f"pass --client-secret {INSTALLED_APP_SECRET}"
        )
    return value


_common_options = [
    click.argument("directory", type=str),
    click.option("--authenticate", is_flag=True, default=None),
    click.option(
        "--client-id",
        type=str,
        default=None,
        help=(
            "Client ID of your own Reddit app, used instead of the one in the config file. "
            f"For --authenticate the app's redirect URI must be {OAUTH_REDIRECT_URI}."
        ),
    ),
    click.option(
        "--client-secret",
        type=str,
        default=None,
        callback=_check_client_secret,
        help=(
            "Secret of the app given with --client-id. Required with --client-id unless the app is an "
            f"installed app or its ID is the one in the config file; pass {INSTALLED_APP_SECRET} to force "
            "installed-app mode."
        ),
    ),
    click.option(
        "--concurrency",
        type=click.IntRange(min=1),
        default=None,
        help="Number of resources to fetch at once. 1 disables concurrency.",
    ),
    click.option("--config", type=str, default=None),
    click.option("--disable-module", multiple=True, default=None, type=str),
    click.option("--exclude-id", default=None, multiple=True),
    click.option("--exclude-id-file", default=None, multiple=True),
    click.option("--file-scheme", default=None, type=str),
    click.option("--filename-restriction-scheme", type=click.Choice(("linux", "windows")), default=None),
    click.option("--folder-scheme", default=None, type=str),
    click.option("--ignore-user", type=str, multiple=True, default=None),
    click.option("--include-id-file", multiple=True, default=None),
    click.option("--log", type=str, default=None),
    click.option("--opts", type=str, default=None),
    click.option(
        "--reddit-username",
        type=str,
        default=None,
        help="Your Reddit username. Only used to build the User-Agent Reddit asks API clients to send.",
    ),
    click.option("--saved", is_flag=True, default=None),
    click.option("--search", default=None, type=str),
    click.option("--submitted", is_flag=True, default=None),
    click.option("--subscribed", is_flag=True, default=None),
    click.option("--time-format", type=str, default=None),
    click.option("--upvoted", is_flag=True, default=None),
    click.option("--user-agent", type=str, default=None, help="Send this User-Agent to Reddit instead of the default."),
    click.option("-L", "--limit", default=None, type=int),
    click.option("-l", "--link", multiple=True, default=None, type=str),
    click.option("-m", "--multireddit", multiple=True, default=None, type=str),
    click.option(
        "-S", "--sort", type=click.Choice(("hot", "top", "new", "controversial", "rising", "relevance")), default=None
    ),
    click.option("-s", "--subreddit", multiple=True, default=None, type=str),
    click.option("-t", "--time", type=click.Choice(("all", "hour", "day", "week", "month", "year")), default=None),
    click.option("-u", "--user", type=str, multiple=True, default=None),
    click.option("-v", "--verbose", default=None, count=True),
]

_downloader_options = [
    click.option("--make-hard-links", is_flag=True, default=None),
    click.option("--max-wait-time", type=int, default=None),
    click.option("--no-dupes", is_flag=True, default=None),
    click.option(
        "--recheck",
        is_flag=True,
        default=None,
        help=(
            "Check again the posts that an earlier run with the same settings downloaded in full, "
            "instead of skipping them. Restores files deleted since."
        ),
    ),
    click.option("--search-existing", is_flag=True, default=None),
    click.option("--skip", default=None, multiple=True),
    click.option("--skip-domain", default=None, multiple=True),
    click.option("--skip-subreddit", default=None, multiple=True),
    click.option("--min-score", type=int, default=None),
    click.option("--max-score", type=int, default=None),
    click.option("--min-score-ratio", type=float, default=None),
    click.option("--max-score-ratio", type=float, default=None),
]

_archiver_options = [
    click.option("--all-comments", is_flag=True, default=None),
    click.option("--comment-context", is_flag=True, default=None),
    click.option("-f", "--format", type=click.Choice(("xml", "json", "yaml")), default=None),
]


def _add_options(opts: list):
    def wrap(func):
        for opt in opts:
            func = opt(func)
        return func

    return wrap


def _check_version(context, param, value):
    if not value or context.resilient_parsing:
        return
    current = __version__
    latest = requests.get("https://pypi.org/pypi/bdfr/json", timeout=REQUEST_TIMEOUT).json()["info"]["version"]
    print(f"You are currently using v{current} the latest is v{latest}")
    context.exit()


@click.group()
@click.help_option("-h", "--help")
@click.option(
    "--version",
    is_flag=True,
    is_eager=True,
    expose_value=False,
    callback=_check_version,
    help="Check version and exit.",
)
def cli():
    """BDFR is used to download and archive content from Reddit."""
    pass


@cli.command("download")
@_add_options(_common_options)
@_add_options(_downloader_options)
@click.help_option("-h", "--help")
@click.pass_context
def cli_download(context: click.Context, **_):
    """Used to download content posted to Reddit."""
    config = Configuration()
    config.process_click_arguments(context)
    silence_module_loggers()
    stream = make_console_logging_handler(config.verbose)
    try:
        reddit_downloader = RedditDownloader(config, [stream])
        reddit_downloader.download()
    except Exception:
        logger.exception("Downloader exited unexpectedly")
        raise
    else:
        logger.info("Program complete")


@cli.command("archive")
@_add_options(_common_options)
@_add_options(_archiver_options)
@click.help_option("-h", "--help")
@click.pass_context
def cli_archive(context: click.Context, **_):
    """Used to archive post data from Reddit."""
    config = Configuration()
    config.process_click_arguments(context)
    silence_module_loggers()
    stream = make_console_logging_handler(config.verbose)
    try:
        reddit_archiver = Archiver(config, [stream])
        reddit_archiver.download()
    except Exception:
        logger.exception("Archiver exited unexpectedly")
        raise
    else:
        logger.info("Program complete")


@cli.command("clone")
@_add_options(_common_options)
@_add_options(_archiver_options)
@_add_options(_downloader_options)
@click.help_option("-h", "--help")
@click.pass_context
def cli_clone(context: click.Context, **_):
    """Combines archive and download commands."""
    config = Configuration()
    config.process_click_arguments(context)
    silence_module_loggers()
    stream = make_console_logging_handler(config.verbose)
    try:
        reddit_scraper = RedditCloner(config, [stream])
        reddit_scraper.download()
    except Exception:
        logger.exception("Scraper exited unexpectedly")
        raise
    else:
        logger.info("Program complete")


@cli.command("completion")
@click.argument("shell", type=click.Choice(("all", "bash", "fish", "zsh"), case_sensitive=False), default="all")
@click.help_option("-h", "--help")
@click.option("-u", "--uninstall", is_flag=True, default=False, help="Uninstall completion")
def cli_completion(shell: str, uninstall: bool):
    """\b
    Installs shell completions for BDFR.
    Options: all, bash, fish, zsh
    Default: all"""
    shell = shell.lower()
    if sys.platform == "win32":
        print("Completions are not currently supported on Windows.")
        return
    if uninstall and click.confirm(f"Would you like to uninstall {shell} completions for BDFR"):
        Completion(shell).uninstall()
        return
    if shell not in ("all", "bash", "fish", "zsh"):
        print(f"{shell} is not a valid option.")
        print("Options: all, bash, fish, zsh")
        return
    if click.confirm(f"Would you like to install {shell} completions for BDFR"):
        Completion(shell).install()


def make_console_logging_handler(verbosity: int) -> logging.StreamHandler:
    class StreamExceptionFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            result = not (record.levelno == logging.ERROR and record.exc_info)
            return result

    logger.setLevel(1)
    stream = logging.StreamHandler(sys.stdout)
    stream.addFilter(StreamExceptionFilter())

    formatter = logging.Formatter("[%(asctime)s - %(name)s - %(levelname)s] - %(message)s")
    stream.setFormatter(formatter)

    if verbosity <= 0:
        stream.setLevel(logging.INFO)
    elif verbosity == 1:
        stream.setLevel(logging.DEBUG)
    else:
        stream.setLevel(9)
    return stream


def silence_module_loggers():
    logging.getLogger("praw").setLevel(logging.CRITICAL)
    logging.getLogger("prawcore").setLevel(logging.CRITICAL)
    logging.getLogger("urllib3").setLevel(logging.CRITICAL)


if __name__ == "__main__":
    cli()
