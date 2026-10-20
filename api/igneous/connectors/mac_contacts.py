"""macOS Contacts connector (Contacts.app, which is where iPhone contacts sync). Read-only (CO-2).

Each card becomes a `gcontacts`-style item: one human, their name, and every phone number and email on the
card. The spine turns that into SAME_AS ground truth and names the matching Person nodes, so a Messages
handle like "+16145551234" shows as the contact's name and joins the same person's email identity.

Needs Full Disk Access for the terminal, same as the Messages connector.
"""
import glob
import os
import re
import sqlite3
from collections.abc import Iterator

from igneous.connectors.base import Checkpoint, SourceItem

SOURCES = os.path.expanduser("~/Library/Application Support/AddressBook/Sources/*/AddressBook-v22.abcddb")


def norm_phone(p: str | None) -> str | None:
    """E.164-ish key for matching: '+1 (614) 555-1234' and '6145551234' -> '+16145551234' (US default)."""
    d = re.sub(r"\D", "", p or "")
    if len(d) == 10:
        return "+1" + d
    if len(d) == 11 and d.startswith("1"):
        return "+" + d
    return ("+" + d) if len(d) > 11 else None


class MacContactsConnector:
    name = "maccontacts"

    def __init__(self, pattern: str = SOURCES):
        self.pattern = pattern

    def sync(self, cursor: dict) -> Iterator[SourceItem | Checkpoint]:
        # Small (hundreds of cards): a full pass each time; content hashing makes unchanged cards free.
        for path in sorted(glob.glob(self.pattern)):
            try:
                db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            except sqlite3.OperationalError as e:
                raise PermissionError(f"can't open {path} ({e}); grant your terminal Full Disk Access") from e
            phones: dict[int, list[str]] = {}
            for owner, num in db.execute("SELECT ZOWNER, ZFULLNUMBER FROM ZABCDPHONENUMBER WHERE ZFULLNUMBER IS NOT NULL"):
                if (n := norm_phone(num)):
                    phones.setdefault(owner, []).append(n)
            emails: dict[int, list[str]] = {}
            for owner, em in db.execute("SELECT ZOWNER, ZADDRESS FROM ZABCDEMAILADDRESS WHERE ZADDRESS IS NOT NULL"):
                emails.setdefault(owner, []).append(em.strip().lower())
            source_id = os.path.basename(os.path.dirname(path))
            for pk, uid, first, last, org in db.execute(
                    "SELECT Z_PK, ZUNIQUEID, ZFIRSTNAME, ZLASTNAME, ZORGANIZATION FROM ZABCDRECORD"):
                name = " ".join(x for x in (first, last) if x) or org
                if not name or not (phones.get(pk) or emails.get(pk)):
                    continue
                yield SourceItem(
                    source="maccontacts", external_id=f"{source_id}/{uid or pk}", title=name, body_text="",
                    mime="text/vcard",
                    raw_meta={"emails": sorted(set(emails.get(pk, []))), "phones": sorted(set(phones.get(pk, []))),
                              "orgs": [org] if org else []})
        yield Checkpoint({})
