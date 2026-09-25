#!/usr/bin/env python3

"""Per-thread HTTP sessions for media downloads and site-downloader requests.

A bare `requests.get()` builds a throwaway `Session`, and with it a new
connection pool, for every call, so every download paid for a fresh TCP connect
and TLS handshake. For a typical i.redd.it image that setup was most of the
request time. Reusing a session keeps connections to each host open between
downloads.

`requests.Session` is not documented as thread-safe, so rather than share one
between the download workers each thread gets its own. A worker handles many
submissions in turn, so its session still sees plenty of reuse.

Reddit API traffic does not come through here: PRAW keeps its own session, and
the OAuth and version-check requests are one-off calls with nothing to reuse.
"""

import http.cookiejar
import logging
import threading

import requests
from requests.adapters import HTTPAdapter

logger = logging.getLogger(__name__)

# Number of hosts whose connections a session keeps open. A run touches a
# handful (i.redd.it, preview.redd.it, imgur, the Redgifs API and CDN, ...);
# once more are in use the least recently used pool is dropped.
POOL_CONNECTIONS = 16

# Connections kept per host. A session belongs to one thread, which has at most
# one request in flight, so one would do; the second covers a streamed response
# that is still open when the next request starts.
POOL_MAXSIZE = 2

_local = threading.local()


def _new_session() -> requests.Session:
    session = requests.Session()
    # A throwaway session never carried cookies from one download to the next.
    # Keep it that way, so a cookie set by one post's host cannot change what a
    # later post receives. Cookies still flow within a single request's redirect
    # chain, and per-call `cookies=` arguments still apply.
    session.cookies = requests.cookies.RequestsCookieJar(policy=http.cookiejar.DefaultCookiePolicy(allowed_domains=[]))
    # The default adapter's retry setting (none) is kept deliberately: callers
    # own their retry and backoff behaviour.
    adapter = HTTPAdapter(pool_connections=POOL_CONNECTIONS, pool_maxsize=POOL_MAXSIZE)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def get_session() -> requests.Session:
    """Return the calling thread's session, creating it on first use."""
    session = getattr(_local, "session", None)
    if session is None:
        session = _new_session()
        _local.session = session
        logger.log(9, f"Created HTTP session for thread {threading.current_thread().name}")
    return session


def close_session() -> None:
    """Close the calling thread's session, if it has one.

    The next `get_session()` call on this thread starts a fresh one.
    """
    session = getattr(_local, "session", None)
    if session is not None:
        del _local.session
        session.close()
