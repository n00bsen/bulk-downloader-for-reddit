#!/usr/bin/env python3

"""Shared constants for BDFR.

These live in their own module so that both the download path and the
concurrency/GUI layers can import them without creating import cycles.
"""

# Timeout, in seconds, applied to every outgoing HTTP request. A connect/read
# timeout matters far more once downloads run concurrently: without one, a
# single unresponsive host occupies a worker slot forever.
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 60
REQUEST_TIMEOUT = (CONNECT_TIMEOUT, READ_TIMEOUT)

# Default number of worker threads fetching resources within a single job.
DEFAULT_CONCURRENCY = 4

# Default number of jobs the GUI runs simultaneously.
DEFAULT_MAX_PARALLEL_JOBS = 3

# Where Reddit sends the browser back to after an OAuth2 login. BDFR listens on
# this port during --authenticate, so a user's own Reddit app must be registered
# with exactly this redirect URI.
OAUTH_REDIRECT_PORT = 7634
OAUTH_REDIRECT_URI = f"http://localhost:{OAUTH_REDIRECT_PORT}"

# Suffix used for partially written files. A download is written to
# `<name><suffix>` and atomically renamed into place once complete, so an
# interrupted run never leaves a truncated file that looks finished.
PARTIAL_SUFFIX = ".bdfrpart"
