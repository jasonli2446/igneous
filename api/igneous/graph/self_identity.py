"""Work out who the account owner is, from identity signals only (never from a name).

The Messages connector already records the user as the Person `me`. This links `me` to every other
identity that is provably the same human, with one SAME_AS edge per signal (extractor 'self', which
resolve.refresh_canonical trusts):

  google_account   the Google account the user signed in with (oauth_tokens)
  gmail_sent       the From address on Gmail messages labeled SENT (send-as aliases included)
  imessage_handle  the phone numbers / Apple IDs the user's own iMessages went out from
  me_card          every number and email on the Contacts card macOS marks as the owner's

Addresses that merely carry the user's name (calendar import feeds, notifications@github.com) are not the
user; they are marked as services (props.kind='service') and never linked. Rebuilt from scratch each run.
"""
import glob
import json
import os
import re
import sqlite3

from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous.connectors.mac_contacts import SOURCES as CONTACT_DBS
from igneous.connectors.mac_contacts import norm_phone

EXTRACTOR = "self"
CHAT_DB = os.path.expanduser("~/Library/Messages/chat.db")

# Protocol facts, not user rules: these addresses are automated senders even when they wear a person's name.
SERVICE_DOMAINS = ("import.calendar.google.com", "group.calendar.google.com", "group.v.calendar.google.com",
                   "resource.calendar.google.com")
SERVICE_LOCAL = re.compile(r"^(no-?reply|notifications?|mailer-daemon|do-?not-?reply|bounces?)([+.-].*)?$", re.I)


def is_service_address(addr: str) -> bool:
    if "@" not in addr:
        # SMS short codes (banks, verification codes, delivery alerts) are 5-6 digits: automated, never a person.
        return bool(re.fullmatch(r"\d{3,6}", addr.strip()))
    local, domain = addr.lower().rsplit("@", 1)
    if domain.endswith("users.noreply.github.com"):
        return False  # a real person's GitHub commit address
    return domain.endswith(SERVICE_DOMAINS) or bool(SERVICE_LOCAL.match(local))


def _signals(s: Session) -> list[tuple[str, str]]:
    """(signal, identity key) pairs. Keys match how spine keys Person nodes."""
    out: list[tuple[str, str]] = []
    try:
        for (acct,) in s.execute(text("SELECT account FROM oauth_tokens WHERE provider = 'google'")):
            out.append(("google_account", acct.lower()))
    except Exception:  # no Google connected (the table is created on first connect)
        s.rollback()
    for (addr,) in s.execute(text("""
        SELECT DISTINCT lower(raw_meta->'from'->0->>'email') FROM source_items
        WHERE source = 'gmail' AND raw_meta->'labels' @> '["SENT"]'
    """)):
        if addr:
            out.append(("gmail_sent", addr))
    if os.path.exists(CHAT_DB):
        try:
            db = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True)
            for (h,) in db.execute("""
                SELECT DISTINCT destination_caller_id FROM message
                WHERE is_from_me = 1 AND destination_caller_id IS NOT NULL
            """):
                key = h.lower() if "@" in h else norm_phone(h)
                if key:
                    out.append(("imessage_handle", key))
        except sqlite3.OperationalError:
            pass  # no Full Disk Access: the other signals still work
    for path in glob.glob(CONTACT_DBS):
        try:
            db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            me = [r[0] for r in db.execute("SELECT Z_PK FROM ZABCDRECORD WHERE ZCONTAINERWHERECONTACTISME IS NOT NULL")]
            for pk in me:
                for (num,) in db.execute("SELECT ZFULLNUMBER FROM ZABCDPHONENUMBER WHERE ZOWNER = ?", (pk,)):
                    if (n := norm_phone(num)):
                        out.append(("me_card", n))
                for (em,) in db.execute("SELECT ZADDRESS FROM ZABCDEMAILADDRESS WHERE ZOWNER = ?", (pk,)):
                    if em:
                        out.append(("me_card", em.strip().lower()))
        except sqlite3.OperationalError:
            continue
    return out


def run(s: Session) -> dict:
    s.execute(text("DELETE FROM edges WHERE type = 'SAME_AS' AND extractor = :ex"), {"ex": EXTRACTOR})
    # Mark automated senders before linking anything, so a feed named "Jason Li Calendar" can't become the user.
    services = 0
    for nid, key in s.execute(text("SELECT id, key FROM nodes WHERE type = 'Person'")).all():
        if is_service_address(key):
            s.execute(text("""UPDATE nodes SET props = props || '{"kind": "service"}' WHERE id = :id"""), {"id": nid})
            services += 1

    me = s.execute(text("""
        INSERT INTO nodes (type, key, name, props) VALUES ('Person', 'me', 'Me', '{"self": true}')
        ON CONFLICT (type, key) DO UPDATE SET props = nodes.props || '{"self": true}' RETURNING id
    """)).scalar_one()
    linked: dict[str, list[str]] = {}
    for signal, key in _signals(s):
        if key == "me" or ("@" in key and is_service_address(key)):
            continue
        node = s.execute(text("SELECT id FROM nodes WHERE type = 'Person' AND key = :k"), {"k": key}).scalar()
        if node is None:
            continue  # an identity that never appears in the data has nothing to merge
        a, b = sorted((me, node))
        s.execute(text("""
            INSERT INTO edges (src, dst, type, extractor, confidence, props)
            VALUES (:a, :b, 'SAME_AS', :ex, 1.0, CAST(:p AS jsonb))
            ON CONFLICT (src, dst, type, extractor) DO UPDATE
              -- parenthesized: || binds tighter than ->, which made this NULL
              SET props = jsonb_set(edges.props, '{signals}',
                                    COALESCE(edges.props->'signals', '[]'::jsonb) || (EXCLUDED.props->'signals'))
        """), {"a": a, "b": b, "ex": EXTRACTOR, "p": json.dumps({"signals": [signal]})})
        linked.setdefault(key, []).append(signal)
    s.commit()
    return {"self_links": len(linked), "signals": sum(len(v) for v in linked.values()), "services": services}
