"""The Enron demo mailboxes (maildir). `parse_message` turns one RFC 822 message into a SourceItem; Gmail parses
what it fetches with it too, so both sources share fingerprints, threading and quoted-reply recovery."""
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
        # Enron's X-From, X-To, X-cc and X-bcc carry the display names its address headers lack (graph/spine.py,
        # `x_names`).
        "x_from": str(msg.get("x-from", "")) or None,
        "folder": str(msg.get("x-folder", "")) or None,
    }
    # The recipients' names, stored only when present so other mail keeps its content hash.
    for header in ("x-to", "x-cc", "x-bcc"):
        if value := str(msg.get(header, "")).strip():
            meta[header.replace("-", "_")] = value
    # Bulk-mail markers, stored only when present so ordinary messages keep their content hash.
    if msg.get("list-unsubscribe"):
        meta["list_unsubscribe"] = True
    if precedence := str(msg.get("precedence", "")).strip().lower():
        meta["precedence"] = precedence
    return SourceItem(source="email", external_id=external_id, title=str(msg.get("subject", "")).strip(),
                      body_text=body, occurred_at=occurred, mime="message/rfc822", raw_meta=meta)


class MaildirConnector:
    """Walks <root>/<mailbox>/... with each mailbox's files in path order. The cursor holds, per mailbox, the last
    path fully processed, so syncing another mailbox starts that one from its beginning."""
    name = "email"

    def __init__(self, root: Path, mailboxes: list[str] | None = None):
        """`mailboxes` None reads every mailbox under `root`."""
        self.root = Path(root)
        self.mailboxes = mailboxes

    def _boxes(self) -> list[str]:
        found = sorted(p.name for p in self.root.iterdir() if p.is_dir())
        if self.mailboxes is None:
            return found
        if missing := sorted(set(self.mailboxes) - set(found)):
            raise ValueError(f"No mailbox {', '.join(missing)} in {self.root} (it has {len(found)}).")
        return sorted(set(self.mailboxes))

    def _paths(self, box: str) -> list[str]:
        # Sorted as the strings the cursor compares, never as Path objects (which order "a/1" before "a-b/1").
        return sorted(p.relative_to(self.root).as_posix() for p in (self.root / box).rglob("*") if p.is_file())

    def sync(self, cursor: dict) -> Iterator[SourceItem | Checkpoint]:
        done = dict(cursor.get("mailboxes") or {})
        if legacy := cursor.get("last_path"):  # one position shared by every mailbox: it holds for its own only
            done.setdefault(legacy.split("/", 1)[0], legacy)
        n = 0
        for box in self._boxes():
            after = done.get(box)
            for rel in self._paths(box):
                if after and rel <= after:
                    continue
                yield parse_message((self.root / rel).read_bytes(), external_id=rel)
                done[box] = rel
                n += 1
                if n % CHECKPOINT_EVERY == 0:
                    yield Checkpoint({"mailboxes": dict(done)})
        yield Checkpoint({"mailboxes": done})
