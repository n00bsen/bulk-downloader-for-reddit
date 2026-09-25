#!/usr/bin/env python3

import logging
from argparse import Namespace
from pathlib import Path

import click
import yaml

from bdfr.constants import DEFAULT_CONCURRENCY, DEFAULT_MAX_PARALLEL_JOBS

logger = logging.getLogger(__name__)


class Configuration(Namespace):
    def __init__(self):
        super().__init__()
        self.authenticate = False
        self.concurrency: int = DEFAULT_CONCURRENCY
        self.config = None
        self.opts: str | None = None
        self.directory: str = "."
        # Look at posts the download record lists as done instead of skipping them.
        self.recheck: bool = False
        self.disable_module: list[str] = []
        self.exclude_id = []
        self.exclude_id_file = []
        self.file_scheme: str = "{REDDITOR}_{TITLE}_{POSTID}"
        self.filename_restriction_scheme = None
        self.folder_scheme: str = "{SUBREDDIT}"
        self.ignore_user = []
        self.include_id_file = []
        self.limit: int | None = None
        self.link: list[str] = []
        self.log: str | None = None
        self.make_hard_links = False
        self.max_wait_time = None
        self.multireddit: list[str] = []
        self.no_dupes: bool = False
        self.saved: bool = False
        self.search: str | None = None
        self.search_existing: bool = False
        self.skip: list[str] = []
        self.skip_domain: list[str] = []
        self.skip_subreddit: list[str] = []
        self.min_score = None
        self.max_score = None
        self.min_score_ratio = None
        self.max_score_ratio = None
        self.sort: str = "hot"
        self.submitted: bool = False
        self.subscribed: bool = False
        self.subreddit: list[str] = []
        self.time: str = "all"
        self.time_format = None
        self.upvoted: bool = False
        self.user: list[str] = []
        self.verbose: int = 0

        # Reddit API credentials. None means "use what the config file holds",
        # which by default is the client ID bundled with BDFR and shared by
        # every user. reddit_username only feeds the User-Agent string.
        self.client_id: str | None = None
        self.client_secret: str | None = None
        self.user_agent: str | None = None
        self.reddit_username: str | None = None

        # GUI-specific options
        self.max_parallel_jobs: int = DEFAULT_MAX_PARALLEL_JOBS

        # Archiver-specific options
        self.all_comments = False
        self.format = "json"
        self.comment_context: bool = False

    def __repr__(self) -> str:
        # Namespace's repr lists every attribute, and a Configuration ends up
        # inside other reprs (the GUI's Job dataclass, exception messages), so
        # mask the secret here rather than trusting every caller not to log it.
        shown = dict(vars(self))
        if shown.get("client_secret"):
            shown["client_secret"] = "***"
        fields = ", ".join(f"{key}={value!r}" for key, value in shown.items())
        return f"{type(self).__name__}({fields})"

    def process_click_arguments(self, context: click.Context):
        if context.params.get("opts") is not None:
            self.parse_yaml_options(context.params["opts"])
        for arg_key in context.params.keys():
            if not hasattr(self, arg_key):
                logger.warning(f"Ignoring an unknown CLI argument: {arg_key}")
                continue
            val = context.params[arg_key]
            if val is None or val == ():
                # don't overwrite with an empty value
                continue
            setattr(self, arg_key, val)

    def parse_yaml_options(self, file_path: str):
        yaml_file_loc = Path(file_path)
        if not yaml_file_loc.exists():
            logger.error(f"No YAML file found at {yaml_file_loc}")
            return
        with yaml_file_loc.open() as file:
            try:
                opts = yaml.safe_load(file)
            except yaml.YAMLError as e:
                logger.error(f"Could not parse YAML options file: {e}")
                return
        for arg_key, val in opts.items():
            if not hasattr(self, arg_key):
                logger.warning(f"Ignoring an unknown YAML argument: {arg_key}")
                continue
            setattr(self, arg_key, val)
