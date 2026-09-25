#!/usr/bin/env python3

import configparser
import functools
import importlib.resources
import io
import itertools
import logging
import logging.handlers
import os
import platform
import re
import shutil
from abc import ABCMeta, abstractmethod
from collections.abc import Callable, Iterable
from datetime import datetime
from enum import Enum, auto
from pathlib import Path
from time import sleep

import appdirs
import praw
import praw.exceptions
import praw.models
import prawcore

from bdfr import __version__
from bdfr import exceptions as errors
from bdfr.configuration import Configuration
from bdfr.constants import OAUTH_REDIRECT_URI
from bdfr.download_filter import DownloadFilter
from bdfr.file_name_formatter import FileNameFormatter
from bdfr.listing import ResumableListing, call_with_retry
from bdfr.locking import atomic_write, file_lock
from bdfr.oauth2 import OAuth2Authenticator, OAuth2TokenManager
from bdfr.site_authenticator import SiteAuthenticator

logger = logging.getLogger(__name__)

# Marks an error that must survive into a GUI job's final message: a source
# dropped during setup otherwise leaves the job looking finished with 0 downloads.
RUN_SUMMARY = {"bdfr_event": "run_summary"}

# Reddit usernames are 3-20 letters, digits, underscores or hyphens.
_REDDIT_USERNAME = re.compile(r"[A-Za-z0-9_-]{3,20}")

# Records which client ID obtained config.cfg's user_token. Reddit only refreshes
# a token for the app that obtained it.
TOKEN_CLIENT_ID_OPTION = "user_token_client_id"

# Given as the client secret, selects an installed app, which has none. An empty
# argument cannot do that everywhere: Windows PowerShell 5.1 silently drops it.
INSTALLED_APP_SECRET = "none"


def _text(value: object) -> str:
    """Return a credential-like option as stripped text.

    A YAML --opts file loads an all-digit value such as `reddit_username: 1234567`
    as an int, and it has to be handled as text rather than crash the run.
    """
    return "" if value is None else str(value).strip()


def build_user_agent(reddit_username: str | None = None, override: str | None = None) -> str:
    """Return the User-Agent BDFR sends to Reddit.

    Reddit's API rules ask for "<platform>:<app ID>:<version> (by /u/<username>)"
    and throttle generic agents harder. BDFR used to send the machine's hostname,
    which is neither that format nor something a user should have to publish.
    """
    user_agent = _text(override)
    if user_agent:
        # A line break would let the value inject further HTTP headers, and a
        # character http.client cannot encode fails the first request. Either
        # only surfaces deep inside PRAW, where it reads as a per-user failure.
        if not user_agent.isascii() or any(ord(char) < 32 or ord(char) == 127 for char in user_agent):
            raise errors.BulkDownloaderException("The User-Agent must be a single line of plain ASCII text")
        return user_agent
    user_agent = f"{platform.system().lower() or 'unknown'}:bdfr:{__version__}"
    username = _normalise_reddit_username(reddit_username)
    if username:
        user_agent += f" (by /u/{username})"
    return user_agent


def _normalise_reddit_username(raw: str | None) -> str | None:
    name = _text(raw).strip("/")
    if name.lower().startswith("u/"):
        name = name[2:]
    if not name:
        return None
    if not _REDDIT_USERNAME.fullmatch(name):
        # Only the length is logged: in the GUI this field sits right under the
        # secret, so a rejected value may well be a secret pasted into it.
        logger.warning(
            f"Leaving the {len(name)}-character Reddit username out of the User-Agent: "
            "a username is 3-20 letters, digits, underscores or hyphens"
        )
        return None
    return name


def resolve_reddit_credentials(args: Configuration, cfg_parser: configparser.ConfigParser) -> tuple[str, str | None]:
    """Pick the Reddit client ID and secret; explicit arguments beat the config file.

    A secret belongs to the ID it was issued with, so an explicitly given ID only
    inherits the config file's secret when it is the ID the config file holds:
    the user's own ID paired with the bundled secret could only fail to
    authenticate. A blank secret, or INSTALLED_APP_SECRET, means an "installed
    app", which has none, and PRAW has to be given None for it to use the
    installed-client grant rather than sending an empty password.
    """
    config_id = _text(cfg_parser.get("DEFAULT", "client_id", fallback=None))
    explicit_id = _text(args.client_id)
    client_id = explicit_id or config_id
    client_secret = args.client_secret
    if client_secret is None and client_id == config_id:
        client_secret = cfg_parser.get("DEFAULT", "client_secret", fallback=None)
    if not client_id:
        raise errors.BulkDownloaderException(
            "No Reddit client ID is configured. Pass --client-id or set client_id in the config file"
        )
    client_secret = _text(client_secret)
    if client_secret.lower() == INSTALLED_APP_SECRET:
        client_secret = ""
    return client_id, client_secret or None


@functools.cache
def _bundled_client_id() -> str | None:
    parser = configparser.ConfigParser()
    try:
        parser.read_string(importlib.resources.files("bdfr").joinpath("default_config.cfg").read_text(encoding="utf-8"))
    except (OSError, configparser.Error):
        return None
    return parser.get("DEFAULT", "client_id", fallback=None)


def _describe_client_id(client_id: str) -> str:
    if client_id == _bundled_client_id():
        return "the client ID bundled with BDFR"
    return f"a user-supplied client ID starting {client_id[:4]!r}"


def describe_reddit_client(client_id: str, client_secret: str | None) -> str:
    """Say which Reddit app is in use without revealing its credentials."""
    kind = "installed app, no secret" if client_secret is None else "app with a secret"
    return f"{_describe_client_id(client_id)} ({kind})"


def check_token_matches_client(
    cfg_parser: configparser.ConfigParser, client_id: str, config_location: Path | None
) -> None:
    """Refuse a stored refresh token that another Reddit app obtained.

    Reddit rejects a refresh token presented with any other client ID, and PRAW
    reports that as a bare HTTP error on the first request, which says nothing
    about the cause. Tokens saved before the issuing ID was recorded came from
    config.cfg's own client_id, since that was the only way to choose an app.
    """
    config_id = _text(cfg_parser.get("DEFAULT", "client_id", fallback=None))
    issuer = _text(cfg_parser.get("DEFAULT", TOKEN_CLIENT_ID_OPTION, fallback=None)) or config_id
    if not issuer or issuer == client_id:
        return
    if client_id == config_id:
        # The config file's app, usually the bundled one: the user has no ID or
        # secret of it to pass, and needs none, since it is used by default.
        how_to_log_in = "run BDFR once from a terminal with --authenticate and without --client-id"
    else:
        how_to_log_in = (
            "run BDFR once from a terminal with --authenticate and this app's --client-id and --client-secret. "
            f"The app's redirect URI must be {OAUTH_REDIRECT_URI}"
        )
    raise errors.RedditAuthenticationError(
        f"The saved Reddit login was made with {_describe_client_id(issuer)}, but this run uses "
        f"{_describe_client_id(client_id)}. Reddit only accepts a login from the app that made it, so either "
        f"go back to that app, or log in with this one: delete the user_token line from {config_location} and "
        f"{how_to_log_in}."
    )


def raise_if_credentials_rejected(error: Exception, reddit: praw.Reddit) -> None:
    """Raise one clear, fatal error if `error` means Reddit refused the app's credentials.

    PRAW reports that as a bare 401 or OAuth error on the first request. Left
    alone, BDFR logged it against each user in turn, waited a minute after each,
    and then finished as if the run had simply found nothing. Every request uses
    the same credentials, so none of the others can succeed either.
    """
    if isinstance(error, prawcore.ResponseException):
        if error.response.status_code != 401:
            return
    elif not isinstance(error, prawcore.OAuthException):
        return
    client_id = _text(reddit.config.client_id)
    # Read from PRAW, never logged or shown: only whether there is one matters.
    secret = reddit.config.client_secret
    client_secret = secret if isinstance(secret, str) and secret.strip() else None
    message = (
        f"Reddit rejected the API credentials of {describe_reddit_client(client_id, client_secret)}: {error}. "
        "Check the client ID and secret: a script or web app needs its secret, and an installed app has none."
    )
    if client_id == _bundled_client_id():
        message += (
            " Unless the bundled secret was changed, Reddit may have withdrawn the app that all BDFR users share:"
            " register your own app and pass it with --client-id, or enter it in the desktop application's"
            " Reddit API section."
        )
    if not reddit.read_only:
        message += (
            " If they are right, the saved login may have been revoked: delete the user_token line from the config"
            " file and log in again with --authenticate."
        )
    raise errors.RedditAuthenticationError(message) from error


class RedditTypes:
    class SortType(Enum):
        CONTROVERSIAL = auto()
        HOT = auto()
        NEW = auto()
        RELEVENCE = auto()
        RISING = auto()
        TOP = auto()

    class TimeType(Enum):
        ALL = "all"
        DAY = "day"
        HOUR = "hour"
        MONTH = "month"
        WEEK = "week"
        YEAR = "year"


class RedditConnector(metaclass=ABCMeta):
    # Set by determine_log_path(); only an exclusively owned log is rolled over.
    owns_log_file: bool = False

    def __init__(self, args: Configuration, logging_handlers: Iterable[logging.Handler] = ()):
        self.args = args
        self.config_directories = appdirs.AppDirs("bdfr", "BDFR")
        self.determine_directories()
        self.load_config()
        self.read_config()
        file_log = self.create_file_logger()
        self._apply_logging_handlers(itertools.chain(logging_handlers, [file_log]))
        self.run_time = datetime.now().isoformat()
        self._setup_internal_objects()

        self.reddit_lists = self.retrieve_reddit_lists()

    def _setup_internal_objects(self):

        self.parse_disabled_modules()

        self.download_filter = self.create_download_filter()
        logger.log(9, "Created download filter")
        self.time_filter = self.create_time_filter()
        logger.log(9, "Created time filter")
        self.sort_filter = self.create_sort_filter()
        logger.log(9, "Created sort filter")
        self.file_name_formatter = self.create_file_name_formatter()
        logger.log(9, "Create file name formatter")

        self.create_reddit_instance()
        self.args.user = list(filter(None, [self.resolve_user_name(user) for user in self.args.user]))

        self.excluded_submission_ids = set.union(
            self.read_id_files(self.args.exclude_id_file),
            set(self.args.exclude_id),
        )

        self.args.link = list(itertools.chain(self.args.link, self.read_id_files(self.args.include_id_file)))

        self.master_hash_list = {}
        self.authenticator = self.create_authenticator()
        logger.log(9, "Created site authenticator")

        self.args.skip_subreddit = self.split_args_input(self.args.skip_subreddit)
        self.args.skip_subreddit = {sub.lower() for sub in self.args.skip_subreddit}

    @staticmethod
    def _apply_logging_handlers(handlers: Iterable[logging.Handler]):
        main_logger = logging.getLogger()
        for handler in handlers:
            main_logger.addHandler(handler)

    def read_config(self):
        """Read any cfg values that need to be processed"""
        if self.args.max_wait_time is None:
            self.args.max_wait_time = self.cfg_parser.getint("DEFAULT", "max_wait_time", fallback=120)
            logger.debug(f"Setting maximum download wait time to {self.args.max_wait_time} seconds")
        if self.args.time_format is None:
            option = self.cfg_parser.get("DEFAULT", "time_format", fallback="ISO")
            if re.match(r"^[\s\'\"]*$", option):
                option = "ISO"
            logger.debug(f"Setting datetime format string to {option}")
            self.args.time_format = option
        if not self.args.disable_module:
            self.args.disable_module = [self.cfg_parser.get("DEFAULT", "disabled_modules", fallback="")]
        if not self.args.filename_restriction_scheme:
            self.args.filename_restriction_scheme = self.cfg_parser.get(
                "DEFAULT", "filename_restriction_scheme", fallback=None
            )
            logger.debug(f"Setting filename restriction scheme to '{self.args.filename_restriction_scheme}'")
        self.write_config_if_changed()

    def write_config_if_changed(self):
        """Persist the in-memory config, but only when it differs from what is on disk.

        Writing unconditionally on every startup means two concurrent runs race on
        the same file. Comparing first makes the common case a no-op, and the
        lock plus atomic replace makes the rare real write safe. The text is
        always ConfigParser's default "key = value" form, the same as
        OAuth2TokenManager writes: with two formats in use, each writer would
        undo the other and the comparison would almost never match.
        """
        config_path = Path(self.config_location)
        buffer = io.StringIO()
        self.cfg_parser.write(buffer)
        rendered = buffer.getvalue()
        try:
            with file_lock(config_path):
                try:
                    existing = config_path.read_text(encoding="utf-8")
                except OSError:
                    existing = None
                if existing == rendered:
                    logger.log(9, "Configuration on disk is already up to date")
                    return
                atomic_write(config_path, rendered)
                logger.log(9, f"Written updated configuration to {config_path}")
        except errors.BulkDownloaderException as e:
            # A config that cannot be updated is not fatal: the run can proceed
            # with the values already loaded into memory.
            logger.warning(f"Could not update configuration at {config_path}: {e}")

    def parse_disabled_modules(self):
        disabled_modules = self.args.disable_module
        disabled_modules = self.split_args_input(disabled_modules)
        disabled_modules = {name.strip().lower() for name in disabled_modules}
        self.args.disable_module = disabled_modules
        logger.debug(f"Disabling the following modules: {', '.join(self.args.disable_module)}")

    def create_reddit_instance(self):
        client_id, client_secret = resolve_reddit_credentials(self.args, self.cfg_parser)
        user_agent = build_user_agent(self.args.reddit_username, self.args.user_agent)
        logger.debug(f"Using {describe_reddit_client(client_id, client_secret)}")
        logger.debug(f"Using User-Agent {user_agent!r}")
        praw_settings = {
            "client_id": client_id,
            "client_secret": client_secret,
            "user_agent": user_agent,
            # PRAW otherwise asks PyPI for a newer release once per process,
            # and the GUI starts a process for every job.
            "check_for_updates": False,
        }
        if self.args.authenticate:
            logger.debug("Using authenticated Reddit instance")
            if self.cfg_parser.has_option("DEFAULT", "user_token"):
                check_token_matches_client(self.cfg_parser, client_id, self.config_location)
            else:
                logger.log(9, "Commencing OAuth2 authentication")
                scopes = self.cfg_parser.get("DEFAULT", "scopes", fallback="identity, history, read, save")
                scopes = OAuth2Authenticator.split_scopes(scopes)
                oauth2_authenticator = OAuth2Authenticator(scopes, client_id, client_secret, user_agent=user_agent)
                token = oauth2_authenticator.retrieve_new_token()
                self.cfg_parser["DEFAULT"]["user_token"] = token
                self.cfg_parser["DEFAULT"][TOKEN_CLIENT_ID_OPTION] = client_id
                self.write_config_if_changed()
            token_manager = OAuth2TokenManager(self.cfg_parser, self.config_location)

            self.authenticated = True
            self.reddit_instance = praw.Reddit(**praw_settings, token_manager=token_manager)
        else:
            logger.debug("Using unauthenticated Reddit instance")
            self.authenticated = False
            self.reddit_instance = praw.Reddit(**praw_settings)

    def retrieve_reddit_lists(self) -> list[Iterable]:
        master_list = []
        master_list.extend(self.get_subreddits())
        logger.log(9, "Retrieved subreddits")
        master_list.extend(self.get_multireddits())
        logger.log(9, "Retrieved multireddits")
        master_list.extend(self.get_user_data())
        logger.log(9, "Retrieved user data")
        master_list.extend(self.get_submissions_from_link())
        logger.log(9, "Retrieved submissions for given links")
        return master_list

    def determine_directories(self):
        self.download_directory = Path(self.args.directory).resolve().expanduser()
        self.config_directory = Path(self.config_directories.user_config_dir)

        self.download_directory.mkdir(exist_ok=True, parents=True)
        self.config_directory.mkdir(exist_ok=True, parents=True)

    def load_config(self):
        self.cfg_parser = configparser.ConfigParser()
        if self.args.config:
            if (cfg_path := Path(self.args.config)).exists():
                self.cfg_parser.read(cfg_path)
                self.config_location = cfg_path
                return
        possible_paths = [
            Path("./config.cfg"),
            Path("./default_config.cfg"),
            Path(self.config_directory, "config.cfg"),
            Path(self.config_directory, "default_config.cfg"),
        ]
        self.config_location = None
        for path in possible_paths:
            if path.resolve().expanduser().exists():
                self.config_location = path
                logger.debug(f"Loading configuration from {path}")
                break
        if not self.config_location:
            with importlib.resources.as_file(importlib.resources.files("bdfr").joinpath("default_config.cfg")) as path:
                self.config_location = path
                shutil.copy(self.config_location, Path(self.config_directory, "default_config.cfg"))
        if not self.config_location:
            raise errors.BulkDownloaderException("Could not find a configuration file to load")
        self.cfg_parser.read(self.config_location)

    def create_file_logger(self) -> logging.handlers.RotatingFileHandler:
        log_path = self.determine_log_path()
        backup_count = self.cfg_parser.getint("DEFAULT", "backup_log_count", fallback=3)
        file_handler = logging.handlers.RotatingFileHandler(
            log_path,
            mode="a",
            backupCount=backup_count,
        )
        # Only roll over a log this process is going to own exclusively. The
        # default path is per-process, so there is nothing to roll over; a
        # user-supplied path may be shared, and rolling it over would destroy
        # another running instance's log.
        if self.owns_log_file and log_path.exists() and log_path.stat().st_size > 0:
            try:
                file_handler.doRollover()
            except PermissionError:
                logger.warning(
                    f"Could not roll over logfile at {log_path}, appending instead. "
                    "Another BDFR process may be using it."
                )
        formatter = logging.Formatter("[%(asctime)s - %(name)s - %(levelname)s] - %(message)s")
        file_handler.setFormatter(formatter)
        file_handler.setLevel(0)
        return file_handler

    def determine_log_path(self) -> Path:
        """Pick the log file for this run.

        The default is per-process so that several simultaneous downloads each
        get their own log instead of fighting over one file. An explicit --log
        is always honoured exactly as given.
        """
        if self.args.log is not None:
            log_path = Path(self.args.log).resolve().expanduser()
            if not log_path.parent.exists():
                raise errors.BulkDownloaderException("Designated location for logfile does not exist")
            self.owns_log_file = True
            return log_path
        self.owns_log_file = False
        return Path(self.config_directory, f"log_output.{os.getpid()}.txt")

    @staticmethod
    def sanitise_subreddit_name(subreddit: str) -> str:
        pattern = re.compile(r"^(?:https://www\.reddit\.com/)?(?:r/)?(.*?)/?$")
        match = re.match(pattern, subreddit)
        if not match:
            raise errors.BulkDownloaderException(f"Could not find subreddit name in string {subreddit}")
        return match.group(1)

    @staticmethod
    def split_args_input(entries: list[str]) -> set[str]:
        all_entries = []
        split_pattern = re.compile(r"[,;]\s?")
        for entry in entries:
            results = re.split(split_pattern, entry)
            all_entries.extend([RedditConnector.sanitise_subreddit_name(name) for name in results])
        return set(all_entries)

    def get_subreddits(self) -> list[ResumableListing]:
        out = []
        subscribed_subreddits = set()
        if self.args.subscribed:
            if self.args.authenticate:
                try:
                    subscribed_subreddits = call_with_retry(
                        lambda: {s.display_name for s in self.reddit_instance.user.subreddits(limit=None)},
                        "the list of subscribed subreddits",
                        sleep=sleep,
                    )
                except prawcore.InsufficientScope:
                    logger.error("BDFR has insufficient scope to access subreddit lists")
                except prawcore.PrawcoreException as e:
                    raise_if_credentials_rejected(e, self.reddit_instance)
                    raise
            else:
                logger.error("Cannot find subscribed subreddits without an authenticated instance")
        if self.args.subreddit or subscribed_subreddits:
            for reddit in self.split_args_input(self.args.subreddit) | subscribed_subreddits:
                if reddit == "friends" and self.authenticated is False:
                    logger.error("Cannot read friends subreddit without an authenticated instance")
                    continue
                try:
                    reddit = self.reddit_instance.subreddit(reddit)
                    try:
                        check = functools.partial(self.check_subreddit_status, reddit)
                        call_with_retry(check, f"r/{reddit}", sleep=sleep)
                    except errors.RedditAuthenticationError:
                        # Every other source would be refused the same way.
                        raise
                    except errors.BulkDownloaderException as e:
                        logger.error(str(e), extra=RUN_SUMMARY)
                        continue
                    if self.args.search:
                        search = functools.partial(
                            reddit.search,
                            self.args.search,
                            sort=self.sort_filter.name.lower(),
                            time_filter=self.time_filter.value,
                        )
                        description = f'posts in r/{reddit} matching "{self.args.search}"'
                        out.append(ResumableListing(search, self.args.limit, description, sleep=sleep))
                        logger.debug(
                            f'Added submissions from subreddit {reddit} with the search term "{self.args.search}"'
                        )
                    else:
                        out.append(self.create_filtered_listing_generator(reddit, f"posts of r/{reddit}"))
                        logger.debug(f"Added submissions from subreddit {reddit}")
                except errors.RedditAuthenticationError:
                    raise
                except (errors.BulkDownloaderException, praw.exceptions.PRAWException, prawcore.PrawcoreException) as e:
                    # call_with_retry has already waited out what it could; an outage or
                    # rate limit that outlasted it costs this subreddit, not the whole run.
                    raise_if_credentials_rejected(e, self.reddit_instance)
                    logger.error(f"Failed to get submissions for subreddit {reddit}: {e}", extra=RUN_SUMMARY)
        return out

    def resolve_user_name(self, in_name: str) -> str:
        if in_name == "me":
            if self.authenticated:
                try:
                    resolved_name = call_with_retry(
                        self.reddit_instance.user.me, "the logged-in user's profile", sleep=sleep
                    ).name
                except prawcore.PrawcoreException as e:
                    raise_if_credentials_rejected(e, self.reddit_instance)
                    raise
                logger.log(9, f"Resolved user to {resolved_name}")
                return resolved_name
            else:
                logger.warning('To use "me" as a user, an authenticated Reddit instance must be used')
        else:
            return in_name

    def get_submissions_from_link(self) -> list[list[praw.models.Submission]]:
        supplied_submissions = []
        for sub_id in self.args.link:
            if len(sub_id) in (6, 7):
                supplied_submissions.append(self.reddit_instance.submission(id=sub_id))
            else:
                supplied_submissions.append(self.reddit_instance.submission(url=sub_id))
        return [supplied_submissions]

    def determine_sort_function(self) -> Callable:
        if self.sort_filter is RedditTypes.SortType.NEW:
            sort_function = praw.models.Subreddit.new
        elif self.sort_filter is RedditTypes.SortType.RISING:
            sort_function = praw.models.Subreddit.rising
        elif self.sort_filter is RedditTypes.SortType.CONTROVERSIAL:
            sort_function = praw.models.Subreddit.controversial
        elif self.sort_filter is RedditTypes.SortType.TOP:
            sort_function = praw.models.Subreddit.top
        else:
            sort_function = praw.models.Subreddit.hot
        return sort_function

    def get_multireddits(self) -> list[ResumableListing]:
        if self.args.multireddit:
            if len(self.args.user) != 1:
                logger.error("Only 1 user can be supplied when retrieving from multireddits")
                return []
            out = []
            for name in self.split_args_input(self.args.multireddit):
                description = f"multireddit {name} of u/{self.args.user[0]}"
                try:
                    multi = self.reddit_instance.multireddit(redditor=self.args.user[0], name=name)
                    # Reading the subreddits fetches the multireddit, a setup lookup like a user's.
                    if not call_with_retry(functools.partial(getattr, multi, "subreddits"), description, sleep=sleep):
                        raise errors.BulkDownloaderException
                    out.append(self.create_filtered_listing_generator(multi, f"posts of {description}"))
                    logger.debug(f"Added submissions from multireddit {multi}")
                except (errors.BulkDownloaderException, praw.exceptions.PRAWException, prawcore.PrawcoreException) as e:
                    raise_if_credentials_rejected(e, self.reddit_instance)
                    logger.error(f"Failed to get submissions for multireddit {name}: {e}", extra=RUN_SUMMARY)
            return out
        else:
            return []

    def create_filtered_listing_generator(self, reddit_source, description: str | None = None) -> ResumableListing:
        sort_function = self.determine_sort_function()
        if self.sort_filter in (RedditTypes.SortType.TOP, RedditTypes.SortType.CONTROVERSIAL):
            factory = functools.partial(sort_function, reddit_source, time_filter=self.time_filter.value)
        else:
            factory = functools.partial(sort_function, reddit_source)
        return ResumableListing(factory, self.args.limit, description or str(reddit_source), sleep=sleep)

    def get_user_data(self) -> list[ResumableListing]:
        if any([self.args.submitted, self.args.upvoted, self.args.saved]):
            if not self.args.user:
                logger.warning("At least one user must be supplied to download user data")
                return []
            generators = []
            for user in self.args.user:
                try:
                    try:
                        call_with_retry(
                            functools.partial(self.check_user_existence, user), f"the profile of u/{user}", sleep=sleep
                        )
                    except errors.RedditAuthenticationError:
                        # Every other user would be refused the same way, so
                        # neither log it per user nor wait before the next.
                        raise
                    except errors.BulkDownloaderException as e:
                        logger.error(str(e), extra=RUN_SUMMARY)
                        continue
                    redditor = self.reddit_instance.redditor(user)
                    if self.args.submitted:
                        logger.debug(f"Retrieving submitted posts of user {user}")
                        generators.append(
                            self.create_filtered_listing_generator(redditor.submissions, f"submitted posts of u/{user}")
                        )
                    if not self.authenticated and any((self.args.upvoted, self.args.saved)):
                        logger.warning("Accessing user lists requires authentication")
                    else:
                        for wanted, kind, factory in (
                            (self.args.upvoted, "upvoted", redditor.upvoted),
                            (self.args.saved, "saved", redditor.saved),
                        ):
                            if wanted:
                                logger.debug(f"Retrieving {kind} posts of user {user}")
                                description = f"{kind} posts of u/{user}"
                                generators.append(ResumableListing(factory, self.args.limit, description, sleep=sleep))
                except prawcore.PrawcoreException as e:
                    # call_with_retry has already waited out rate limits and outages,
                    # so what is left will not clear up by waiting before the next user.
                    logger.error(f"User {user} failed to be retrieved due to a PRAW exception: {e}", extra=RUN_SUMMARY)
            return generators
        else:
            return []

    def check_user_existence(self, name: str):
        user = self.reddit_instance.redditor(name=name)
        try:
            if user.id:
                return
        except prawcore.exceptions.NotFound:
            raise errors.BulkDownloaderException(f"Could not find user {name}") from None
        except prawcore.PrawcoreException as e:
            raise_if_credentials_rejected(e, self.reddit_instance)
            raise
        except AttributeError:
            if hasattr(user, "is_suspended"):
                raise errors.BulkDownloaderException(f"User {name} is banned") from None

    def create_file_name_formatter(self) -> FileNameFormatter:
        return FileNameFormatter(
            self.args.file_scheme, self.args.folder_scheme, self.args.time_format, self.args.filename_restriction_scheme
        )

    def create_time_filter(self) -> RedditTypes.TimeType:
        try:
            return RedditTypes.TimeType[self.args.time.upper()]
        except (KeyError, AttributeError):
            return RedditTypes.TimeType.ALL

    def create_sort_filter(self) -> RedditTypes.SortType:
        try:
            return RedditTypes.SortType[self.args.sort.upper()]
        except (KeyError, AttributeError):
            return RedditTypes.SortType.HOT

    def create_download_filter(self) -> DownloadFilter:
        return DownloadFilter(self.args.skip, self.args.skip_domain)

    def create_authenticator(self) -> SiteAuthenticator:
        return SiteAuthenticator(self.cfg_parser)

    @abstractmethod
    def download(self):
        pass

    @staticmethod
    def check_subreddit_status(subreddit: praw.models.Subreddit):
        if subreddit.display_name in ("all", "friends"):
            return
        try:
            assert subreddit.id
        except prawcore.NotFound:
            raise errors.BulkDownloaderException(f"Source {subreddit.display_name} cannot be found") from None
        except prawcore.Redirect:
            raise errors.BulkDownloaderException(f"Source {subreddit.display_name} does not exist") from None
        except prawcore.Forbidden:
            raise errors.BulkDownloaderException(
                f"Source {subreddit.display_name} is private and cannot be scraped"
            ) from None
        except prawcore.PrawcoreException as e:
            raise_if_credentials_rejected(e, subreddit._reddit)
            raise

    @staticmethod
    def read_id_files(file_locations: list[str]) -> set[str]:
        out = []
        for id_file in file_locations:
            id_file = Path(id_file).resolve().expanduser()
            if not id_file.exists():
                logger.warning(f"ID file at {id_file} does not exist")
                continue
            with id_file.open("r") as file:
                for line in file:
                    out.append(line.strip())
        return set(out)
