"""Gmail connector. Backfill via messages.list, then incremental via history.list from a stored historyId.

Messages are fetched as RFC 822 (`format=raw`) and parsed by the same code as the Enron importer, so Gmail
gets identical fingerprinting, threading, and quoted-reply recovery. Gmail also supplies real thread ids and
In-Reply-To headers, which the spine uses directly.

Spam, trash and drafts are never stored, and neither are Promotions, Social and Forums unless the account syncs
all mail; what is stored follows that rule both ways. The backfill query and the history filter apply it, checked
again against the labels `format=raw` returns. A message that leaves a skipped label (moved to Primary, marked not
spam) is fetched then; one that enters it, or is deleted, is removed; and turning all mail off removes the account's
bulk mail.

The cursor: `history_id` (where the next sync continues), `synced_at` (when the last complete sync began, epoch
seconds), `all_mail` and `after` (the account's choice and the start of its window), and while a backfill runs,
its query, page and offset.
"""
import base64
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from igneous.config import settings
from igneous.connectors.base import Checkpoint, SourceItem, Tombstone
from igneous.connectors.email_maildir import parse_message

CHECKPOINT_EVERY = 50
FETCH_WORKERS = 2  # the quota, not latency, is the bottleneck; 2 in flight is enough to hit the paced rate
# Never stored: not mail you sent or received.
NOT_MAIL = frozenset({"SPAM", "TRASH", "DRAFT"})
# Skipped unless the account syncs all mail: bulk mail that drowns out conversations as evidence.
BULK = frozenset({"CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_FORUMS"})
# Gmail's `after:` compares receive times on Google's clock; going back an hour covers a skewed local clock.
CLOCK_MARGIN = 3600


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


QUOTA = TokenBucket(rate=settings.gmail_units_per_min / 60, burst=50)


def _retry(call, units: float = 5, tries: int = 6):
    """Backoff on rate limits (429 / 403 rateLimitExceeded) and transient 5xx. Google's per-user window is a
    minute, so rate-limit waits grow to 64 s: a shorter cap gives up while the window is still closed."""
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


def skipped_labels(all_mail: bool) -> frozenset[str]:
    """Label ids whose messages a sync doesn't store."""
    return NOT_MAIL if all_mail else NOT_MAIL | BULK


def query(all_mail: bool, after: int | None) -> str:
    """The backfill's messages.list query: the same exclusions as `skipped_labels` (messages.list already leaves
    out spam and trash), from `after` (epoch seconds) on."""
    parts = ["-in:drafts", *([] if all_mail else ["-category:promotions", "-category:social", "-category:forums"])]
    return " ".join([*parts, *([f"after:{after}"] if after else [])])


class GmailConnector:
    name = "gmail"

    def __init__(self, creds, account: str, since_days: int | None = 90, all_mail: bool | None = None,
                 service=None):
        """`since_days` bounds the first sync (None: all mail). `all_mail` None keeps the account's choice
        (Promotions, Social and Forums skipped until it is set)."""
        self.creds = creds
        self.svc = service or build("gmail", "v1", credentials=creds, cache_discovery=False)
        self._local = threading.local()  # googleapiclient services aren't thread-safe: one per thread
        self.account = account
        self.since_days = since_days
        self.all_mail = all_mail

    def _svc(self):
        if self.creds is None:  # an injected service
            return self.svc
        if not hasattr(self._local, "svc"):
            self._local.svc = build("gmail", "v1", credentials=self.creds, cache_discovery=False)
        return self._local.svc

    def _fetch(self, msg_id: str, skip: frozenset[str]) -> SourceItem | Tombstone:
        """The message as an item, or a Tombstone when it is gone or now carries a skipped label (it may have been
        stored before)."""
        try:
            m = _retry(self._svc().users().messages().get(userId="me", id=msg_id, format="raw"))
        except HttpError as e:
            if e.resp.status == 404:  # deleted since it was listed
                return Tombstone(self.name, msg_id)
            raise
        if skip & set(m.get("labelIds", [])):
            return Tombstone(self.name, msg_id)
        item = parse_message(base64.urlsafe_b64decode(m["raw"]), external_id=m["id"])
        item.source = "gmail"
        item.raw_meta.update({"gmail_thread_id": m.get("threadId"), "labels": m.get("labelIds", []),
                              "account": self.account})
        if item.occurred_at is None and m.get("internalDate"):
            item.occurred_at = datetime.fromtimestamp(int(m["internalDate"]) / 1000, UTC)
        return item

    def sync(self, cursor: dict) -> Iterator[SourceItem | Checkpoint | Tombstone]:
        stored_choice = bool(cursor.get("all_mail", False))
        all_mail = stored_choice if self.all_mail is None else self.all_mail
        if "backfill_q" in cursor and all_mail == stored_choice:  # finish an interrupted backfill first
            yield from self._backfill(cursor["backfill_q"], all_mail, cursor.get("after"), resume=cursor)
        elif not cursor.get("history_id"):
            after = int(time.time()) - self.since_days * 86400 if self.since_days else None
            yield from self._backfill(query(all_mail, after), all_mail, after)
        elif all_mail != stored_choice:
            # The choice changed: list the account's window again under it (unchanged messages store nothing).
            # Narrowed, the bulk mail stored under all mail goes too: see _backfill.
            yield from self._backfill(query(all_mail, cursor.get("after")), all_mail, cursor.get("after"),
                                      drop_bulk=not all_mail)
        else:
            try:
                yield from self._history(cursor, all_mail)
            except HttpError as e:
                if e.resp.status != 404:
                    raise
                # The history id expired (Gmail keeps about a week): list everything since the last complete
                # sync. A cursor without that time falls back to the first-sync window.
                since = cursor.get("synced_at")
                gap = int(since) - CLOCK_MARGIN if since else (
                    int(time.time()) - self.since_days * 86400 if self.since_days else None)
                yield from self._backfill(query(all_mail, gap), all_mail, cursor.get("after"))

    def _ids(self, q: str) -> Iterator[str]:
        """The ids of every message `q` matches."""
        page = None
        while True:
            resp = _retry(self.svc.users().messages().list(userId="me", q=q, pageToken=page, maxResults=500,
                                                           includeSpamTrash=False))
            yield from (m["id"] for m in resp.get("messages", []))
            page = resp.get("nextPageToken")
            if not page:
                return

    def _backfill(self, q: str, all_mail: bool, after: int | None, resume: dict | None = None,
                  drop_bulk: bool = False) -> Iterator[SourceItem | Checkpoint | Tombstone]:
        """List what `q` matches and store it. With `drop_bulk` (all mail turned off), then remove the bulk mail of
        the account's window: Gmail ids carry no account, so a CompletedScan scope can't name this account's
        window; Promotions, Social and Forums are listed by label and tombstoned instead."""
        resume = resume or {}
        skip = skipped_labels(all_mail)
        started = resume.get("started") or int(time.time())
        drop_bulk = resume.get("drop_bulk", drop_bulk)
        # Record the history position BEFORE listing, so nothing that arrives mid-backfill is missed. A resumed
        # backfill keeps its original position.
        history_id = resume.get("history_id") or _retry(self.svc.users().getProfile(userId="me"), 1)["historyId"]
        state = {"history_id": history_id, "backfill_q": q, "started": started, "all_mail": all_mail, "after": after,
                 **({"drop_bulk": True} if drop_bulk else {})}
        # The position is (page token, offset in that page), so a resume skips what that page already stored.
        page, skip_n = resume.get("backfill_page"), resume.get("backfill_offset", 0)
        while True:
            resp = _retry(self.svc.users().messages().list(userId="me", q=q or None, pageToken=page,
                                                           maxResults=500, includeSpamTrash=False))
            refs = list(enumerate(resp.get("messages", []), 1))[skip_n:]
            # Fetch in parallel but yield in order, so checkpoints still mean "everything before i is stored".
            with ThreadPoolExecutor(FETCH_WORKERS) as pool:
                fetched = pool.map(lambda r: self._fetch(r[1]["id"], skip), refs)
                for (i, _), event in zip(refs, fetched, strict=True):
                    yield event
                    if i % CHECKPOINT_EVERY == 0:
                        yield Checkpoint({**state, "backfill_page": page, "backfill_offset": i})
            skip_n = 0
            page = resp.get("nextPageToken")
            if not page:
                break
        if drop_bulk:
            bulk = "{category:promotions category:social category:forums}" + (f" after:{after}" if after else "")
            for msg_id in self._ids(bulk):
                yield Tombstone(self.name, msg_id)
        yield Checkpoint({"history_id": history_id, "synced_at": started, "all_mail": all_mail, "after": after})

    def _history(self, cursor: dict, all_mail: bool) -> Iterator[SourceItem | Checkpoint | Tombstone]:
        skip = skipped_labels(all_mail)
        started = int(time.time())
        state = {"all_mail": all_mail, "after": cursor.get("after"), "synced_at": cursor.get("synced_at")}
        start, page, latest, pending = cursor["history_id"], None, cursor["history_id"], 0
        while True:
            resp = _retry(self.svc.users().history().list(
                userId="me", startHistoryId=start, pageToken=page,
                historyTypes=["messageAdded", "messageDeleted", "labelAdded", "labelRemoved"]), 2)
            latest = resp.get("historyId", latest)
            for record in resp.get("history", []):
                for msg_id in _wanted(record, skip):
                    yield self._fetch(msg_id, skip)
                    pending += 1
                for msg_id in _unwanted(record, skip):
                    yield Tombstone(self.name, msg_id)
                # Between records only: a sync resumed from a record's id continues after the whole record.
                if pending >= CHECKPOINT_EVERY:
                    yield Checkpoint({**state, "history_id": record["id"]})
                    pending = 0
            page = resp.get("nextPageToken")
            if not page:
                break
        yield Checkpoint({**state, "history_id": latest, "synced_at": started})


def _wanted(record: dict, skip: frozenset[str]) -> list[str]:
    """Ids to fetch for one history record: messages added outside the skipped labels, and messages that just
    left one (moved out of Promotions or Spam, say) and carry none now."""
    ids = [added["message"]["id"] for added in record.get("messagesAdded", [])
           if not skip & set(added["message"].get("labelIds", []))]
    ids += [removed["message"]["id"] for removed in record.get("labelsRemoved", [])
            if skip & set(removed.get("labelIds", [])) and not skip & set(removed["message"].get("labelIds", []))]
    return list(dict.fromkeys(ids))


def _unwanted(record: dict, skip: frozenset[str]) -> list[str]:
    """Ids to remove for one history record: messages deleted, and messages that just entered a skipped label
    (moved to Spam, Trash or, unless the account syncs all mail, Promotions)."""
    ids = [deleted["message"]["id"] for deleted in record.get("messagesDeleted", [])]
    ids += [added["message"]["id"] for added in record.get("labelsAdded", []) if skip & set(added.get("labelIds", []))]
    return list(dict.fromkeys(ids))
