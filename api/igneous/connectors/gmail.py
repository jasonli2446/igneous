"""Gmail connector. Backfill via messages.list, then incremental via history.list from a stored historyId.

Messages are fetched as RFC 822 (`format=raw`) and parsed by the same code as the Enron importer, so Gmail
gets identical fingerprinting, threading, and quoted-reply recovery. Gmail also supplies real thread ids and
In-Reply-To headers, which the spine uses directly.
"""
import base64
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from igneous.connectors.base import Checkpoint, SourceItem
from igneous.connectors.email_maildir import parse_message

CHECKPOINT_EVERY = 50


def _retry(call, tries: int = 5):
    """Exponential backoff on Gmail's per-user rate limits (429 / 403 rateLimitExceeded / 5xx)."""
    for i in range(tries):
        try:
            return call.execute()
        except HttpError as e:
            status = e.resp.status
            if status in (429, 500, 502, 503) or (status == 403 and b"rateLimit" in (e.content or b"")):
                time.sleep(min(30, 2 ** i))
                continue
            raise
    return call.execute()


class GmailConnector:
    name = "gmail"

    def __init__(self, creds, account: str, since_days: int | None = 90, query: str | None = None):
        self.svc = build("gmail", "v1", credentials=creds, cache_discovery=False)
        self.account = account
        self.since_days = since_days
        self.query = query

    def _fetch(self, msg_id: str) -> SourceItem | None:
        try:
            m = _retry(self.svc.users().messages().get(userId="me", id=msg_id, format="raw"))
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
        history_id = resume.get("history_id") or _retry(self.svc.users().getProfile(userId="me"))["historyId"]
        page, n = resume.get("backfill_page"), 0
        while True:
            resp = _retry(self.svc.users().messages().list(userId="me", q=q or None, pageToken=page,
                                                           maxResults=500, includeSpamTrash=False))
            for ref in resp.get("messages", []):
                item = self._fetch(ref["id"])
                if item:
                    yield item
                n += 1
                if n % CHECKPOINT_EVERY == 0:
                    # Mid-backfill checkpoint: resume the listing from this page next time.
                    yield Checkpoint({"history_id": history_id, "backfill_page": page, "backfill_q": q})
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
                    userId="me", startHistoryId=start, pageToken=page, historyTypes=["messageAdded"]))
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
