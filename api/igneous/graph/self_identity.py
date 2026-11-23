"""Work out who the owner of the graph is, from identity signals only (never from a name).

The owner is the Person `me`. Sources that are yours by nature create it (Messages, AI chats, Maps visits, your
LinkedIn profile: graph/spine.py), and this stage links it to every identity that is provably the same human, with
one SAME_AS edge per signal (extractor 'self', which resolve.refresh_canonical trusts):

  google_account   the Google account the user signed in with (oauth_tokens)
  gmail_sent       the From address on Gmail messages labeled SENT (send-as aliases included)
  archive_sent     a mail archive's owner: the address that sent most of each mailbox's sent folders, when every
                   mailbox in the archive has the same one (one person's mail, not several people's)
  imessage_handle  the phone numbers / Apple IDs the user's own iMessages went out from
  me_card          every number and email on the Contacts card macOS marks as the owner's

With no signal, no source that created `me` and no fact you stated about yourself, there is no owner node: nothing
says whose graph this is (an archive of several people's mailboxes, say), and the explorer has no "you" to open on.

Addresses that merely carry the user's name (calendar import feeds, notifications@github.com) are not the user; they
are marked as services (props.kind='service') and never linked, and so are one-way senders of bulk mail. Links and
service marks are recomputed each run, so a mark whose reason went away is cleared.
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
from igneous.graph import identity
from igneous.graph.identity import OWNER_KEY, OWNER_NAME, self_id
from igneous.graph.organizations import PERSONAL_DOMAINS

EXTRACTOR = "self"
CHAT_DB = os.path.expanduser("~/Library/Messages/chat.db")

# Protocol facts, not user rules: these addresses are automated senders even when they wear a person's name.
SERVICE_DOMAINS = ("import.calendar.google.com", "group.calendar.google.com", "group.v.calendar.google.com",
                   "resource.calendar.google.com")
SERVICE_LOCAL = re.compile(r"^(no-?reply|notifications?|mailer-daemon|do-?not-?reply|bounces?)([+.-].*)?$"
                           r"|(^|[-_.])no-?reply([-_.+]|$)", re.I)  # comments-noreply@, drive-shares-dm-noreply@
# Senders that relay a real person's name for a product: "Sam Lee (Google Docs)", "Alex (via Google Chat)".
RELAY_NAME = re.compile(r"\((via |Google |from )[^)]*\)\s*$", re.I)
RELAY_DOMAINS = ("docs.google.com", "drive.google.com", "calendar-server.bounces.google.com")

BOT_LOGIN = re.compile(r"(\[bot\]|-bot|^bot-|^dependabot|^renovate|^github-actions)$|\[bot\]", re.I)

# A mail archive's sent folders, as the maildir connector names records (`<mailbox>/<folder>/.../<file>`, see
# connectors/email_maildir.py): Enron's sent, sent_items, _sent_mail and _sent; Maildir++'s .Sent; "Sent Messages".
SENT_FOLDER = r"^[._]?sent([ _-]?(items|mail|messages))?$"

# Lowercase words between the parts of a name: Ludwig van Beethoven, Maria de la Cruz.
NAME_PARTICLES = frozenset({"van", "von", "de", "del", "della", "der", "den", "da", "di", "du", "la", "le", "ter",
                            "bin", "ibn", "al", "el", "dos", "das", "do", "y"})
NAME_WORD = re.compile(r"[^\W\d_]+(?:['\u2019-][^\W\d_]+)*\.?")  # letters, inner apostrophes and hyphens, an initial's dot


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
    """A display name a product put on someone else's behalf ("Sam Lee (Google Docs)")."""
    return bool(name and RELAY_NAME.search(name))


def looks_like_person_name(name: str | None) -> bool:
    """A display name shaped like a human's: two to four capitalized words of letters ("José García", "Mary Ann
    Smith", "Jean-Luc Picard", "Siobhan O'Brien", "Mary A. Smith"), with lowercase particles allowed between them
    ("Vincent van Gogh"). Not "Adam from Bolt", "ALERTS", "Netflix" or "Team 42"."""
    words = (name or "").split()
    if not words or words[0].lower() in NAME_PARTICLES or words[-1].lower() in NAME_PARTICLES:
        return False
    core = [w for w in words if w not in NAME_PARTICLES]
    if not 2 <= len(core) <= 4:
        return False
    for word in core:
        letters = re.sub(r"\W", "", word)
        if not NAME_WORD.fullmatch(word) or not word[0].isupper() or (len(letters) > 1 and letters.isupper()):
            return False
    return True


ARCHIVE_OWNER = text("""
WITH mail AS (
    SELECT split_part(external_id, '/', 1) AS mailbox, split_part(external_id, '/', 2) AS folder,
           lower(raw_meta->'from'->0->>'email') AS sender
    FROM source_items WHERE source = 'email'),
sent AS (SELECT mailbox, sender, count(*) AS n FROM mail WHERE folder ~* :sent_folder AND sender IS NOT NULL
         GROUP BY mailbox, sender),
owners AS (SELECT s.mailbox, s.sender FROM sent s
           WHERE 2 * s.n > (SELECT sum(t.n) FROM sent t WHERE t.mailbox = s.mailbox))
SELECT (SELECT count(DISTINCT mailbox) FROM mail) AS mailboxes, count(*) AS owned,
       array_agg(DISTINCT sender) AS senders
FROM owners
""")


def archive_owner(s: Session) -> str | None:
    """The address that owns the mail archive (source 'email'): in each mailbox, the address that sent most of what
    its sent folders hold (its owner, not an assistant who sometimes sent for them), when every mailbox has one and it
    is the same address. Several people's mailboxes have no owner."""
    row = s.execute(ARCHIVE_OWNER, {"sent_folder": SENT_FOLDER}).one()
    if not row.mailboxes or row.owned != row.mailboxes or len(row.senders or []) != 1:
        return None
    owner = row.senders[0]
    return None if is_service_address(owner) else owner


def _signals(s: Session) -> list[tuple[str, str]]:
    """(signal, identity key) pairs. Keys match how spine keys Person nodes."""
    out: list[tuple[str, str]] = [("google_account", acct.lower()) for (acct,) in s.execute(text(
        "SELECT account FROM oauth_tokens WHERE provider = 'google'"))]
    for (addr,) in s.execute(text("""
        SELECT DISTINCT lower(raw_meta->'from'->0->>'email') FROM source_items
        WHERE source = 'gmail' AND raw_meta->'labels' @> '["SENT"]'
    """)):
        if addr:
            out.append(("gmail_sent", addr))
    if owner := archive_owner(s):
        out.append(("archive_sent", owner))
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
# Identities count as one person by trusted links only (`known`: node -> cluster), never by the last run's scoring.
ONE_WAY_SENDERS = text("""
WITH known AS (SELECT * FROM unnest(CAST(:nodes AS bigint[]), CAST(:roots AS bigint[])) AS k(node, root)),
me AS (SELECT unnest(CAST(:mine AS bigint[])) AS id
       UNION SELECT k.node FROM known k JOIN known m ON m.root = k.root WHERE m.node = ANY(CAST(:mine AS bigint[]))),
wrote AS (SELECT DISTINCT r.dst AS p FROM edges s JOIN edges r ON r.src = s.dst AND r.type = 'RECEIVED'
          WHERE s.type = 'SENT' AND s.src IN (SELECT id FROM me)),
i_wrote AS (SELECT DISTINCT COALESCE(k.root, wrote.p) AS p FROM wrote LEFT JOIN known k ON k.node = wrote.p),
-- Domains you write to people at (your school, your employers): someone there who emails you is a colleague
-- even if you never wrote to them personally, not a service.
my_domains AS (SELECT DISTINCT split_part(n.key, '@', 2) AS d FROM wrote JOIN nodes n ON n.id = wrote.p
               WHERE n.key LIKE '%@%' AND split_part(n.key, '@', 2) <> ALL(:personal)),
sent AS (SELECT e.src AS p, count(*) AS n,
                count(*) FILTER (WHERE si.item_class IN ('notification', 'marketing')) AS bulk
         FROM edges e JOIN nodes m ON m.id = e.dst JOIN source_items si ON si.id = m.source_item_id
         WHERE e.type = 'SENT' GROUP BY e.src)
SELECT p.id, p.name, sent.n, sent.bulk
FROM sent JOIN nodes p ON p.id = sent.p LEFT JOIN known k ON k.node = p.id
WHERE p.type = 'Person' AND p.key LIKE '%@%' AND sent.bulk >= 0.5 * sent.n
  AND NOT COALESCE((p.props->>'self')::bool, false)
  AND p.props->>'named_by' IS DISTINCT FROM 'contacts'
  AND COALESCE(k.root, p.id) NOT IN (SELECT p FROM i_wrote)
  AND split_part(p.key, '@', 2) NOT IN (SELECT d FROM my_domains)
""")


def one_way_senders(s: Session, mine: list[int]) -> set[int]:
    """People you (any of `mine`, your identities, and those trusted links join to them) never wrote to, at any of
    their trusted identities, outside your domains and your contacts, who send you mostly bulk mail: all of it bulk at
    80%, or at 50% when their name isn't even shaped like a person's."""
    known = identity.known(s)
    return {r.id for r in s.execute(ONE_WAY_SENDERS, {"mine": mine, "personal": sorted(PERSONAL_DOMAINS),
                                                      "nodes": list(known), "roots": list(known.values())})
            if r.bulk >= 0.8 * r.n or not looks_like_person_name(r.name)}


def run(s: Session) -> dict:
    """Recompute the owner's links and every service mark. A derived stage: it leaves the commit to the caller."""
    services: dict[int, str] = {}
    # Automated senders first, so a feed named "Sam Lee Calendar" can't become the user.
    for nid, key, name in s.execute(text("SELECT id, key, name FROM nodes WHERE type = 'Person' AND key <> :me"),
                                    {"me": OWNER_KEY}):
        if is_service_address(key):
            services[nid] = "automated address"
        elif is_relay_name(name):
            services[nid] = "relays another person's name"
    found = {}
    for signal, key in _signals(s):
        if key == OWNER_KEY or ("@" in key and is_service_address(key)):
            continue
        node = s.execute(text("SELECT id FROM nodes WHERE type = 'Person' AND key = :k"), {"k": key}).scalar()
        if node is not None:  # an identity that never appears in the data has nothing to merge
            found.setdefault(node, []).append(signal)

    s.execute(text("DELETE FROM edges WHERE type = 'SAME_AS' AND extractor = :ex"), {"ex": EXTRACTOR})
    me = self_id(s)
    built = me is not None and s.execute(text("""
        SELECT EXISTS (SELECT 1 FROM spine_node_support WHERE node_id = :me)
            OR EXISTS (SELECT 1 FROM edges WHERE (src = :me OR dst = :me) AND extractor = 'user')
    """), {"me": me}).scalar()
    if found or built:
        me = s.execute(text("""
            INSERT INTO nodes (type, key, name, props) VALUES ('Person', :me, :name, '{"self": true}')
            ON CONFLICT (type, key) DO UPDATE SET props = nodes.props || '{"self": true}' RETURNING id
        """), {"me": OWNER_KEY, "name": OWNER_NAME}).scalar_one()
        for node, signals in found.items():
            a, b = sorted((me, node))
            s.execute(text("""
                INSERT INTO edges (src, dst, type, extractor, confidence, props)
                VALUES (:a, :b, 'SAME_AS', :ex, 1.0, CAST(:p AS jsonb))
            """), {"a": a, "b": b, "ex": EXTRACTOR, "p": json.dumps({"signals": signals})})
    elif me is not None:
        s.execute(text("DELETE FROM nodes WHERE id = :me"), {"me": me})  # nothing says whose graph this is any more
        me = None

    mine = [me, *found] if me is not None else []  # the links made now count before resolve folds them in
    services.update({nid: "one-way bulk sender" for nid in one_way_senders(s, mine) if nid not in services})
    ids, reasons = sorted(services), [services[i] for i in sorted(services)]
    s.execute(text("""
        UPDATE nodes n SET props = n.props || jsonb_build_object('kind', 'service', 'service_reason', x.why)
        FROM unnest(CAST(:ids AS bigint[]), CAST(:why AS text[])) AS x(id, why)
        WHERE n.id = x.id AND (n.props->>'kind' IS DISTINCT FROM 'service' OR n.props->>'service_reason' IS DISTINCT FROM x.why)
    """), {"ids": ids, "why": reasons})
    cleared = s.execute(text("""
        UPDATE nodes SET props = props - 'kind' - 'service_reason'
        WHERE type = 'Person' AND props->>'kind' = 'service' AND id <> ALL(CAST(:ids AS bigint[]))
    """), {"ids": ids}).rowcount
    return {"owner": me, "self_links": len(found), "signals": sum(len(v) for v in found.values()),
            "services": len(services), "services_cleared": cleared}
