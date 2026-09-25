#!/usr/bin/env python3

import configparser
import io
import logging
import random
import re
import socket
from pathlib import Path

import praw
import requests
from praw.util.token_manager import BaseTokenManager
from prawcore import Authorizer

from bdfr.constants import OAUTH_REDIRECT_PORT, OAUTH_REDIRECT_URI, REQUEST_TIMEOUT
from bdfr.exceptions import BulkDownloaderException, RedditAuthenticationError
from bdfr.locking import atomic_write, file_lock

logger = logging.getLogger(__name__)


class OAuth2Authenticator:
    def __init__(self, wanted_scopes: set[str], client_id: str, client_secret: str | None, user_agent: str):
        # The login is sent with the same User-Agent as the rest of the run, as
        # Reddit's API rules require, rather than a generic placeholder.
        self.user_agent = user_agent
        self._check_scopes(wanted_scopes, user_agent)
        self.scopes = wanted_scopes
        self.client_id = client_id
        self.client_secret = client_secret

    @staticmethod
    def _check_scopes(wanted_scopes: set[str], user_agent: str):
        response = requests.get(
            "https://www.reddit.com/api/v1/scopes.json",
            headers={"User-Agent": user_agent},
            timeout=REQUEST_TIMEOUT,
        )
        known_scopes = [scope for scope, data in response.json().items()]
        known_scopes.append("*")
        for scope in wanted_scopes:
            if scope not in known_scopes:
                raise BulkDownloaderException(f"Scope {scope} is not known to reddit")

    @staticmethod
    def split_scopes(scopes: str) -> set[str]:
        scopes = re.split(r"[,: ]+", scopes)
        return set(scopes)

    def retrieve_new_token(self) -> str:
        reddit = praw.Reddit(
            redirect_uri=OAUTH_REDIRECT_URI,
            user_agent=self.user_agent,
            client_id=self.client_id,
            client_secret=self.client_secret,
            # PRAW's update check is once per process, and this is the first
            # instance created in it, so it would otherwise contact PyPI.
            check_for_updates=False,
        )
        state = str(random.randint(0, 65000))
        url = reddit.auth.url(self.scopes, state, "permanent")
        logger.warning("Authentication action required before the program can proceed")
        logger.warning(f"Authenticate at {url}")

        client = self.receive_connection()
        data = client.recv(1024).decode("utf-8")
        param_tokens = data.split(" ", 2)[1].split("?", 1)[1].split("&")
        params = dict([token.split("=") for token in param_tokens])

        if state != params["state"]:
            self.send_message(client)
            raise RedditAuthenticationError(f"State mismatch in OAuth2. Expected: {state} Received: {params['state']}")
        elif "error" in params:
            self.send_message(client)
            raise RedditAuthenticationError(f"Error in OAuth2: {params['error']}")

        self.send_message(client, "<script>alert('You can go back to terminal window now.')</script>")
        refresh_token = reddit.auth.authorize(params["code"])
        return refresh_token

    @staticmethod
    def receive_connection() -> socket.socket:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("0.0.0.0", OAUTH_REDIRECT_PORT))
        logger.log(9, f"Server listening on 0.0.0.0:{OAUTH_REDIRECT_PORT}")

        server.listen(1)
        client = server.accept()[0]
        server.close()
        logger.log(9, "Server closed")

        return client

    @staticmethod
    def send_message(client: socket.socket, message: str = ""):
        client.send(f"HTTP/1.1 200 OK\r\n\r\n{message}".encode())
        client.close()


class OAuth2TokenManager(BaseTokenManager):
    def __init__(self, config: configparser.ConfigParser, config_location: Path):
        super().__init__()
        self.config = config
        self.config_location = config_location

    def pre_refresh_callback(self, authorizer: Authorizer):
        if authorizer.refresh_token is None:
            if self.config.has_option("DEFAULT", "user_token"):
                authorizer.refresh_token = self.config.get("DEFAULT", "user_token")
                logger.log(9, "Loaded OAuth2 token for authoriser")
            else:
                raise RedditAuthenticationError("No auth token loaded in configuration")

    def post_refresh_callback(self, authorizer: Authorizer):
        """Persist a rotated refresh token.

        This runs whenever PRAW refreshes the access token. Several BDFR
        processes can hit it at once, so the write is serialised and atomic:
        an interleaved plain write here can truncate the config file and lose
        the refresh token entirely, forcing the user to re-authenticate.
        """
        self.config.set("DEFAULT", "user_token", authorizer.refresh_token)
        config_path = Path(self.config_location)
        buffer = io.StringIO()
        self.config.write(buffer, True)
        with file_lock(config_path):
            atomic_write(config_path, buffer.getvalue())
        logger.log(9, f"Written OAuth2 token from authoriser to {config_path}")
