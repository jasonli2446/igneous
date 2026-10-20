"""Gmail connector. Backfill via messages.list, then incremental via history.list from a stored historyId.

Messages are fetched as RFC 822 (`format=raw`) and parsed by the same code as the Enron importer, so Gmail
gets identical fingerprinting, threading, and quoted-reply recovery. Gmail also supplies real thread ids and
In-Reply-To headers, which the spine uses directly.
"""
import base64
import os
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from igneous.connectors.base import Checkpoint, SourceItem
from igneous.connectors.email_maildir import parse_message

CHECKPOINT_EVERY = 50
FETCH_WORKERS = 2  # the quota, not latency, is the bottleneck; 2 in flight is enough to hit the paced rate


class TokenBucket:
    """Client-side pacing so we stay under Gmail's per-user quota instead of tripping it and waiting out the
    penalty window. messages.get costs 5 units, list 5, getProfile 1."""

    def __init__(self, rate: float, burst: float):
        self.rate, self.capacity, self.tokens, self.t = rate, burst, burst, time.monotonic()
        self.lock = threading.Lock()

    def take(self, n: float = 1.0) -> None:
        with self.lock:
            self._take(n)

    def _take(self, n: float) -> None:
        while True:
            now = time.monotonic()
            self.tokens = min(self.capacity, self.tokens + (now - self.t) * self.rate)
            self.t = now
            if self.tokens >= n:
                self.tokens -= n
                return
            time.sleep((n - self.tokens) / self.rate)


# Measured 2026-10-19: this Testing-mode project was cut off at ~730 units in under a minute, about 5% of the
# published 15k/min default. Raise IGNEOUS_GMAIL_UNITS_PER_MIN if your project's quota page shows more.
UNITS_PER_MIN = float(os.environ.get("IGNEOUS_GMAIL_UNITS_PER_MIN", "600"))
QUOTA = TokenBucket(rate=UNITS_PER_MIN / 60, burst=50)


def _retry(call, units: float = 5, tries: int = 6):
    """Backoff on rate limits (429 / 403 rateLimitExceeded) and transient 5xx. Google's per-user window is a
    minute, so rate-limit waits go up to 64 s; a 16 s cap gave up while the window was still closed."""
    for i in range(tries):
        QUOTA.take(units)
        try:
            return call.execute()
        except HttpError as e:
            status = e.resp.status
            limited = status == 429 or (status == 403 and b"ateLimit" in (e.content or b""))
            if limited or status in (500, 502, 503):
                time.sleep(min(64, 2 ** (i + 1)) if limited else min(16, 2 ** i))
                continue
            raise
    QUOTA.take(units)
    return call.execute()


class GmailConnector:
    name = "gmail"

    # 94% of a real inbox here was Promotions/Social/Updates; as evidence it drowns out actual conversations.
    DEFAULT_QUERY = "-category:promotions -category:social -category:forums"

    def __init__(self, creds, account: str, since_days: int | None = 90, query: str | None = DEFAULT_QUERY):
        self.creds = creds
        self.svc = build("gmail", "v1", credentials=creds, cache_discovery=False)
        self._local = threading.local()  # googleapiclient services aren't thread-safe: one per thread
        self.account = account
        self.since_days = since_days
        self.query = query

    def _svc(self):
        if not hasattr(self._local, "svc"):
            self._local.svc = build("gmail", "v1", credentials=self.creds, cache_discovery=False)
        return self._local.svc

    def _fetch(self, msg_id: str) -> SourceItem | None:
        try:
            m = _retry(self._svc().users().messages().get(userId="me", id=msg_id, format="raw"))
        except HttpError as e:
            if e.resp.status == 404:  # deleted between list and get
                return None
            raise
        item = parse_message(base64.urlsafe_b64decode(m["raw"]), external_id=m["id"])
        item.source = "gmail"
        item.raw_meta.update({"gmail_thread_id": m.get("threadId"), "labels": m.get("labelIds", []),
                              "account": self.account})
        if item.occurred_at is None and m.get("internalDate"):
            item.occurred_at = datetime.fromtimestamp(int(m["internalDate"]) / 1000, UTC)
        return item

    def _backfill(self, resume: dict | None = None) -> Iterator[SourceItem | Checkpoint]:
        resume = resume or {}
        q = resume.get("backfill_q")
        if q is None:
            q = self.query or ""
            if self.since_days:
                q = f"{q} after:{(datetime.now(UTC) - timedelta(days=self.since_days)):%Y/%m/%d}".strip()
        # Record the history position BEFORE listing, so nothing that arrives mid-backfill is missed. A
        # resumed backfill keeps the original position.
        history_id = resume.get("history_id") or _retry(self.svc.users().getProfile(userId="me"), 1)["historyId"]
        # The cursor is (page token, offset within that page): checkpoints mid-page used to save only the page,
        # so a resume re-fetched every message already stored on it.
        page, skip = resume.get("backfill_page"), resume.get("backfill_offset", 0)
        while True:
            resp = _retry(self.svc.users().messages().list(userId="me", q=q or None, pageToken=page,
                                                           maxResults=500, includeSpamTrash=False))
            refs = list(enumerate(resp.get("messages", []), 1))[skip:]
            # Fetch in parallel but yield in order, so checkpoints still mean "everything before i is stored".
            with ThreadPoolExecutor(FETCH_WORKERS) as pool:
                items = pool.map(lambda r: self._fetch(r[1]["id"]), refs)
                for (i, _), item in zip(refs, items, strict=True):
                    if item:
                        yield item
                    if i % CHECKPOINT_EVERY == 0:
                        yield Checkpoint({"history_id": history_id, "backfill_page": page, "backfill_offset": i,
                                          "backfill_q": q})
            skip = 0
            page = resp.get("nextPageToken")
            if not page:
                break
        yield Checkpoint({"history_id": history_id})

    def sync(self, cursor: dict) -> Iterator[SourceItem | Checkpoint]:
        if not cursor.get("history_id") or "backfill_page" in cursor:
            yield from self._backfill(cursor if "backfill_page" in cursor else None)
            return
        start, page, latest, n = cursor["history_id"], None, cursor["history_id"], 0
        try:
            while True:
                resp = _retry(self.svc.users().history().list(
                    userId="me", startHistoryId=start, pageToken=page, historyTypes=["messageAdded"]), 2)
                latest = resp.get("historyId", latest)
                for h in resp.get("history", []):
                    for added in h.get("messagesAdded", []):
                        item = self._fetch(added["message"]["id"])
                        if item:
                            yield item
                        n += 1
                        if n % CHECKPOINT_EVERY == 0:
                            yield Checkpoint({"history_id": h["id"]})
                page = resp.get("nextPageToken")
                if not page:
                    break
        except HttpError as e:
            if e.resp.status == 404:  # historyId too old (Gmail keeps about a week): re-list recent mail
                self.since_days = 7
                yield from self._backfill()
                return
            raise
        yield Checkpoint({"history_id": latest})
