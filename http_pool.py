"""One pooled HTTP session per worker thread.

Every outbound request used to open a brand-new connection: a practice's
homepage, its contact page and its about page were three separate TCP and TLS
handshakes to the same host, and every NPI Registry lookup was another. From
outside the US each handshake costs hundreds of milliseconds before a byte of
the page arrives. Measured on 11 real practice pages from this project's own
leads: 34.1 seconds with a fresh connection per page, 14.5 seconds reusing one.

Thread-local rather than shared, because requests.Session is not documented as
safe to use from several threads at once and every pool here is concurrent.
Worker threads in a ThreadPoolExecutor live for the whole run, so each keeps
its own connections warm across every provider it handles.
"""

import threading

import requests
from requests.adapters import HTTPAdapter

USER_AGENT = "Mozilla/5.0"

# Enough for one worker to hold a connection to each of the hosts it touches
# in a run (practice sites, the NPI Registry, the reader relay) without
# evicting the ones it is about to reuse.
POOL_SIZE = 32

_local = threading.local()


def session():
    """This thread's session, created on first use."""
    current = getattr(_local, "session", None)
    if current is None:
        current = requests.Session()
        adapter = HTTPAdapter(pool_connections=POOL_SIZE, pool_maxsize=POOL_SIZE,
                              max_retries=0)
        current.mount("https://", adapter)
        current.mount("http://", adapter)
        current.headers.update({"User-Agent": USER_AGENT})
        _local.session = current
    return current
