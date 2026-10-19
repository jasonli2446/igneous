"""Enron maildir importer — the offline stand-in for Gmail. Emits the same SourceItem shape Gmail will."""
import email
import email.policy
import hashlib
import re
from collections.abc import Iterator
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path

from bs4 import BeautifulSoup

from igneous.connectors.base import Checkpoint, SourceItem

CHECKPOINT_EVERY = 100  # also the granularity of `sync --limit` (it stops at the first checkpoint past the limit)


def _addrs(msg, header: str) -> list[dict]:
    vals = msg.get_all(header, [])
    return [{"name": n.strip(" '\""), "email": a.strip().lower()}
            for n, a in getaddresses([str(v) for v in vals]) if "@" in a]


def _body(msg) -> str:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        content = part.get_content()
    except (LookupError, UnicodeDecodeError):
        content = part.get_payload(decode=True).decode("latin-1", errors="replace")
    if part.get_content_type() == "text/html":
        content = BeautifulSoup(content, "html.parser").get_text("\n")
    return content


def parse_message(raw: bytes, external_id: str) -> SourceItem:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    try:
        occurred = parsedate_to_datetime(msg["date"]) if msg["date"] else None
    except (TypeError, ValueError):
        occurred = None
    refs = re.findall(r"<[^>]+>", str(msg.get("references", "")))
    body = _body(msg)
    sender = (_addrs(msg, "from") or [{"email": ""}])[0]["email"]
    meta = {
        # Enron stores one message in several folders, each copy with its own Message-ID. This identifies
        # the message itself, so the spine can collapse copies into one node.
        "fingerprint": hashlib.sha1(
            f"{sender}|{occurred.isoformat() if occurred else ''}|{msg.get('subject', '')}|{body.strip()}"
            .encode()).hexdigest(),
        "message_id": str(msg.get("message-id", "")).strip() or None,
        "in_reply_to": str(msg.get("in-reply-to", "")).strip() or None,
        "references": refs,
        "from": _addrs(msg, "from"),
        "to": _addrs(msg, "to"),
        "cc": _addrs(msg, "cc"),
        "bcc": _addrs(msg, "bcc"),
        # Enron's X-From/X-To carry the human-readable names the address headers lack.
        "x_from": str(msg.get("x-from", "")) or None,
        "folder": str(msg.get("x-folder", "")) or None,
    }
    return SourceItem(source="email", external_id=external_id, title=str(msg.get("subject", "")).strip(),
                      body_text=body, occurred_at=occurred, mime="message/rfc822", raw_meta=meta)


class MaildirConnector:
    """Walks <root>/<mailbox>/... in sorted order; the cursor is the last path fully processed."""
    name = "email"

    def __init__(self, root: Path, mailboxes: list[str] | None = None):
        self.root = Path(root)
        self.mailboxes = mailboxes

    def _files(self) -> Iterator[Path]:
        boxes = sorted(p.name for p in self.root.iterdir() if p.is_dir())
        for box in boxes:
            if self.mailboxes and box not in self.mailboxes:
                continue
            yield from sorted(p for p in (self.root / box).rglob("*") if p.is_file())

    def sync(self, cursor: dict) -> Iterator[SourceItem | Checkpoint]:
        after = cursor.get("last_path")
        n = 0
        last = after
        for path in self._files():
            rel = str(path.relative_to(self.root))
            if after and rel <= after:
                continue
            yield parse_message(path.read_bytes(), external_id=rel)
            last = rel
            n += 1
            if n % CHECKPOINT_EVERY == 0:
                yield Checkpoint({"last_path": last})
        yield Checkpoint({"last_path": last})
