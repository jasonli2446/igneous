"""Calls to Google's APIs, paced per API and retried.

Each API has its own quota, so each has its own token bucket: a long Gmail backfill doesn't slow Drive or Calendar
down, and each stays under its per-user limit instead of tripping it and waiting out the penalty window. Gmail counts
quota units (messages.get and list cost 5, getProfile 1, history.list 2) against IGNEOUS_GMAIL_UNITS_PER_MIN; the
others count calls.
"""
import errno
import http.client
import ssl
import threading
import time

import httplib2
from google.auth.exceptions import TransportError
from googleapiclient.errors import HttpError

from igneous.config import settings


class TokenBucket:
    """Client-side pacing: `take(n)` waits until n units are available, refilling at `rate` units a second up to
    `burst`."""

    def __init__(self, rate: float, burst: float):
        self.rate, self.capacity, self.tokens, self.t = rate, burst, burst, time.monotonic()
        self.lock = threading.Lock()

    def take(self, n: float = 1.0) -> None:
        with self.lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.t) * self.rate)
                self.t = now
                if self.tokens >= n:
                    self.tokens -= n
                    return
                time.sleep((n - self.tokens) / self.rate)


# A Google Cloud project in Testing mode is cut off near 730 Gmail units a minute; the others' published per-user
# limits are far above these paces (Drive 12,000 queries a minute, Calendar 600, People 90 reads).
BUCKETS = {
    "gmail": TokenBucket(rate=settings.gmail_units_per_min / 60, burst=50),
    "drive": TokenBucket(rate=10, burst=20),
    "calendar": TokenBucket(rate=5, burst=10),
    "people": TokenBucket(rate=1, burst=5),
}


# The socket errors Google's own client (googleapiclient.http) tries again.
_DROPPED = {"ETIMEDOUT", "EPIPE", "ECONNABORTED", "ECONNREFUSED", "ECONNRESET", "WSAETIMEDOUT"}


def _connection_failed(e: Exception) -> bool:
    """A connection that dropped, timed out, was refused or reset, broke its TLS session, couldn't resolve Google's
    host, or ended mid-response (a token refresh's included): another attempt can succeed. A certificate Google's
    can't be verified against never will."""
    if isinstance(e, ssl.SSLCertVerificationError):
        return False
    if isinstance(e, (ssl.SSLError, TimeoutError, ConnectionError, httplib2.ServerNotFoundError,
                      http.client.IncompleteRead, TransportError)):
        return True
    return isinstance(e, OSError) and errno.errorcode.get(e.errno or 0) in _DROPPED


def call(api: str, request, units: float = 1, attempts: int = 7):
    """Execute a googleapiclient request against `api`'s bucket, up to `attempts` times. Backs off on rate limits
    (429, 403 rateLimitExceeded), transient 5xx and a failed connection (`_connection_failed`); Google's per-user
    window is a minute, so rate-limit waits grow to 64 s. The last attempt's error is raised."""
    bucket = BUCKETS[api]
    for attempt in range(attempts):
        bucket.take(units)
        try:
            return request.execute()
        except HttpError as e:
            status = e.resp.status
            limited = status == 429 or (status == 403 and b"ateLimit" in (e.content or b""))
            if not (limited or status in (500, 502, 503)) or attempt == attempts - 1:
                raise
            time.sleep(min(64, 2 ** (attempt + 1)) if limited else min(16, 2 ** attempt))
        except (OSError, httplib2.HttpLib2Error, http.client.HTTPException, TransportError) as e:
            if not _connection_failed(e) or attempt == attempts - 1:
                raise
            time.sleep(min(16, 2 ** attempt))
