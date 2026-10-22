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
SERVICE_LOCAL = re.compile(r"^(no-?reply|notifications?|mailer-daemon|do-?not-?reply|bounces?)([+.-].*)?$"
                           r"|(^|[-_.])no-?reply([-_.+]|$)", re.I)  # comments-noreply@, drive-shares-dm-noreply@
# Senders that relay a real person's name for a product: "Jason Li (Google Docs)", "Kaitlyn (via Google Chat)".
RELAY_NAME = re.compile(r"\((via |Google |from )[^)]*\)\s*$", re.I)
RELAY_DOMAINS = ("docs.google.com", "drive.google.com", "calendar-server.bounces.google.com")


BOT_LOGIN = re.compile(r"(\[bot\]|-bot|^bot-|^dependabot|^renovate|^github-actions)$|\[bot\]", re.I)


def is_service_address(addr: str) -> bool:
    if addr.lower().startswith("github:"):
        return bool(BOT_LOGIN.search(addr.split(":", 1)[1]))  # vercel[bot], dependabot: automation, not people
    if "@" not in addr:
        # SMS short codes (banks, verification codes, delivery alerts) are 5-6 digits: automated, never a person.
        return bool(re.fullmatch(r"\d{3,6}", addr.strip()))
    local, domain = addr.lower().rsplit("@", 1)
    if domain.endswith("users.noreply.github.com"):
        return bool(BOT_LOGIN.search(local))  # a real person's commit address, unless it's a bot's
    return domain.endswith(SERVICE_DOMAINS + RELAY_DOMAINS) or bool(SERVICE_LOCAL.search(local))


def is_relay_name(name: str | None) -> bool:
    """A display name a product put on someone else's behalf ("Jason Li (Google Docs)")."""
    return bool(name and RELAY_NAME.search(name))


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


# A person is a human you correspond with, not an organization that emails you. A sender is a service when the
# contact is one-way (you never wrote to them, they aren't in your contacts, you share no group chat) and what
# they send is mostly notifications or marketing (item_class): airlines, banks, newsletters, "Adam from Bolt".
ONE_WAY_SENDERS = text("""
WITH me AS (SELECT :me AS id UNION SELECT node_id FROM canonical WHERE canonical_id = :me),
i_wrote AS (SELECT DISTINCT COALESCE(c.canonical_id, r.dst) AS p
            FROM edges s JOIN edges r ON r.src = s.dst AND r.type = 'RECEIVED'
            LEFT JOIN canonical c ON c.node_id = r.dst
            WHERE s.type = 'SENT' AND s.src IN (SELECT id FROM me)),
-- Domains you write to people at (your school, your employers): someone there who emails you is a colleague
-- even if you never wrote to them personally, not a service.
my_domains AS (SELECT DISTINCT split_part(n.key, '@', 2) AS d FROM i_wrote JOIN nodes n ON n.id = i_wrote.p
               WHERE n.key LIKE '%@%' AND split_part(n.key, '@', 2) NOT IN
                   ('gmail.com', 'icloud.com', 'yahoo.com', 'hotmail.com', 'outlook.com', 'me.com', 'aol.com')),
sent AS (SELECT e.src AS p, count(*) AS n,
                count(*) FILTER (WHERE si.item_class IN ('notification', 'marketing')) AS bulk
         FROM edges e JOIN nodes m ON m.id = e.dst JOIN source_items si ON si.id = m.source_item_id
         WHERE e.type = 'SENT' GROUP BY e.src)
UPDATE nodes p SET props = p.props || '{"kind": "service", "service_reason": "one-way bulk sender"}'
FROM sent
WHERE p.id = sent.p AND p.type = 'Person' AND p.key LIKE '%@%'
  AND COALESCE(p.props->>'kind', '') <> 'service' AND NOT COALESCE((p.props->>'self')::bool, false)
  AND p.props->>'named_by' IS DISTINCT FROM 'contacts'
  AND COALESCE((SELECT canonical_id FROM canonical WHERE node_id = p.id), p.id) NOT IN (SELECT p FROM i_wrote)
  AND sent.n > 0
  AND split_part(p.key, '@', 2) NOT IN (SELECT d FROM my_domains)
  -- mostly bulk mail, or a company-style sender you never answered whose mail never came to more than you plus
  -- a list (no exchange with any human in it: every message they sent was received by nobody who replied)
  AND (sent.bulk >= 0.8 * sent.n OR (
        p.name !~ '^[A-Z][a-z]+ [A-Z][a-z]+$'          -- not a "First Last" human name
        AND sent.bulk >= 0.5 * sent.n))
""")


def run(s: Session) -> dict:
    s.execute(text("DELETE FROM edges WHERE type = 'SAME_AS' AND extractor = :ex"), {"ex": EXTRACTOR})
    # Mark automated senders before linking anything, so a feed named "Jason Li Calendar" can't become the user.
    services = 0
    for nid, key, name in s.execute(text("SELECT id, key, name FROM nodes WHERE type = 'Person'")).all():
        if key != "me" and (is_service_address(key) or is_relay_name(name)):
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
    services += s.execute(ONE_WAY_SENDERS, {"me": me}).rowcount
    s.commit()
    return {"self_links": len(linked), "signals": sum(len(v) for v in linked.values()), "services": services}
