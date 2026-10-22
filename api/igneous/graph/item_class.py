"""Classify every source item as conversation | event | notification | marketing, from source-native signals only
(no model call, deterministic). Only conversations become Thread/Message nodes that show in traversal; the rest
stay as searchable items and as evidence (a flight confirmation still supports the trip it belongs to).

The class lives on `source_items.item_class`. `apply` classifies new rows, promotes Gmail threads the user
replied in, and mirrors the result onto graph nodes (`Message.props.item_class`, `Thread.props.conversational`, and `props.hidden` on both) so traversal can skip non-conversations with one set-based predicate.
"""
import re

from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous.graph.self_identity import is_relay_name, is_service_address

CONVERSATION, EVENT, NOTIFICATION, MARKETING = "conversation", "event", "notification", "marketing"
BATCH = 2000

# Calendar replies and meeting plumbing: "Accepted: Standup @ Tue", "Jane has joined your meeting".
CALENDAR_RESPONSE = re.compile(r"^\s*(((tentatively )?accepted|declined|tentative|canceled( event)?|cancelled( event)?"
                               r"|updated invitation( with note)?|new time proposed)\s*[:\-]|.*\bhas joined your meeting\b"
                               r"|.*\b(accepted|declined) this invitation\b)", re.I)
CALENDAR_INVITE = re.compile(r"^\s*invitation( from google calendar)?\s*:", re.I)
# Local parts no person writes from, beyond what self_identity already treats as services.
AUTOMATED_LOCAL = re.compile(r"^(alerts?|billing|receipts?|orders?|invoices?|newsletters?|news|updates?|offers?|promo(tion)?s?"
                             r"|marketing|mailer|automated|system|statements?|rewards|deals|digest)([+.\-_].*)?$", re.I)
UNSUBSCRIBE = re.compile(r"\bunsubscribe\b|manage (your )?(email )?(preferences|subscription)", re.I)
UPDATES_LABELS = {"CATEGORY_UPDATES", "CATEGORY_SOCIAL", "CATEGORY_FORUMS"}
SMALL_EXCHANGE = 3  # recipients (to+cc) at or below which a human-sent reply is a personal exchange


def _is_automated(addr: dict) -> bool:
    email = (addr.get("email") or "").lower()
    if not email:
        return False
    if is_service_address(email) or is_relay_name(addr.get("name")):
        return True
    return bool(AUTOMATED_LOCAL.match(email.split("@")[0]))


def _classify_email(title: str, meta: dict, body: str, keys: frozenset[str]) -> str:
    labels = set(meta.get("labels") or [])
    sender = next(iter(meta.get("from") or []), None) or {}
    if labels & {"SENT", "DRAFT"} or (sender.get("email") or "").lower() in keys:
        return CONVERSATION  # the user wrote it
    if CALENDAR_RESPONSE.match(title):
        return NOTIFICATION
    if CALENDAR_INVITE.match(title):
        return EVENT
    bulk = bool(meta.get("list_unsubscribe")) or str(meta.get("precedence") or "").lower() in ("bulk", "list", "junk")
    unsub = bulk or bool(UNSUBSCRIBE.search(body))
    if "CATEGORY_PROMOTIONS" in labels:
        return MARKETING
    updates = bool(labels & UPDATES_LABELS)
    if unsub and not updates:
        return MARKETING  # a newsletter or promo with an unsubscribe link
    if _is_automated(sender):
        return NOTIFICATION
    if updates:
        # Gmail's own sorting says "not a person", unless this looks like a small personal exchange.
        recipients = len(meta.get("to") or []) + len(meta.get("cc") or [])
        if not unsub and meta.get("in_reply_to") and recipients <= SMALL_EXCHANGE:
            return CONVERSATION
        return NOTIFICATION
    return CONVERSATION


def classify(source: str, title: str | None, raw_meta: dict | None, body: str | None,
             user_keys: frozenset[str] | set[str] = frozenset()) -> str | None:
    """The class of one item, or None for sources that aren't communication (files, contacts, places)."""
    meta, title, body = raw_meta or {}, title or "", body or ""
    keys = frozenset(k.lower() for k in user_keys)
    if source in ("gmail", "email"):
        return _classify_email(title, meta, body, keys)
    if source == "imessage":
        people = [p for p in (meta.get("participants") or []) if p] or [meta.get("chat") or ""]
        # SMS short codes (banks, verification codes, delivery alerts) and automated handles never converse.
        return NOTIFICATION if all(is_service_address(p) for p in people) else CONVERSATION
    if source in ("gcal", "takeout_meet"):
        return EVENT
    if source in ("takeout_activity", "takeout_chrome"):
        return NOTIFICATION  # 'activity': searches and page visits are evidence, never a conversation
    if source in ("takeout_chat", "ai_chats", "github"):
        return CONVERSATION
    return None


# --- database side: set-based, idempotent -------------------------------------------------------------------

def user_keys(s: Session) -> frozenset[str]:
    """Addresses the user writes from: Gmail SENT senders plus connected Google accounts."""
    keys = {r[0] for r in s.execute(text("""
        SELECT DISTINCT lower(raw_meta->'from'->0->>'email') FROM source_items
        WHERE source = 'gmail' AND raw_meta->'labels' @> '["SENT"]'""")) if r[0]}
    try:
        keys |= {r[0].lower() for r in s.execute(text("SELECT account FROM oauth_tokens WHERE provider = 'google'"))}
    except Exception:  # the table is created on first connect
        s.rollback()
    return frozenset(k for k in keys if not is_service_address(k))


# Whether the user wrote this item, by each source's own signal.
USER_SENT = """(si.source IN ('ai_chats', 'github')
    OR (si.source = 'gmail' AND si.raw_meta->'labels' @> '["SENT"]')
    OR (si.source = 'imessage' AND si.raw_meta->'senders' @> '["me"]')
    OR (si.source = 'takeout_chat' AND si.raw_meta->'senders' ?| CAST(:keys AS text[]))
    OR lower(si.raw_meta->'from'->0->>'email') = ANY(CAST(:keys AS text[])))"""

PROMOTE_REPLIED_THREADS = text("""
UPDATE source_items si SET item_class = 'conversation'
WHERE si.source = 'gmail' AND si.item_class = 'notification' AND si.raw_meta->>'gmail_thread_id' IN (
    SELECT u.raw_meta->>'gmail_thread_id' FROM source_items u
    WHERE u.source = 'gmail' AND u.raw_meta->'labels' @> '["SENT"]')
""")

SYNC_MESSAGE_CLASS = text("""
UPDATE nodes n SET props = n.props || jsonb_build_object('item_class', si.item_class)
FROM source_items si
WHERE n.type = 'Message' AND n.source_item_id = si.id AND si.item_class IS NOT NULL
  AND n.props->>'item_class' IS DISTINCT FROM si.item_class
""")

# A thread is a conversation when it holds a conversation message and either a second one or a message the
# user wrote; a lone received message nobody answered is an item. A conversation is shown as its own node only
# when it's a group (3+ people): a one-on-one conversation IS the relationship with that person, so it counts
# toward them (conversational stays true; its messages stay visible) but isn't a second node beside them.
SYNC_THREAD_FLAG = text(f"""
WITH t AS (
    SELECT it.dst AS id,
           count(*) FILTER (WHERE COALESCE(m.props->>'item_class', 'conversation') = 'conversation') AS conv,
           bool_or(COALESCE(m.props->>'item_class', 'conversation') = 'conversation' AND COALESCE({USER_SENT}, false))
               AS replied
    FROM edges it JOIN nodes m ON m.id = it.src AND m.type = 'Message'
    LEFT JOIN source_items si ON si.id = m.source_item_id
    WHERE it.type = 'IN_THREAD' GROUP BY it.dst),
people AS (
    SELECT it.dst AS id, count(DISTINCT COALESCE(c.canonical_id, x.dst)) AS n
    FROM edges it JOIN edges x ON x.src = it.src AND x.type = 'RECEIVED'
    LEFT JOIN canonical c ON c.node_id = x.dst
    WHERE it.type = 'IN_THREAD' GROUP BY it.dst),
flag AS (SELECT t.id, (conv >= 2 OR (conv >= 1 AND replied)) AS ok, COALESCE(people.n, 0) >= 3 AS grp
         FROM t LEFT JOIN people USING (id))
UPDATE nodes th SET props = th.props || jsonb_build_object('conversational', flag.ok,
                                                           'hidden', NOT (flag.ok AND flag.grp))
FROM flag WHERE th.id = flag.id AND th.type = 'Thread'
  AND (th.props->'conversational' IS DISTINCT FROM to_jsonb(flag.ok)
       OR th.props->'hidden' IS DISTINCT FROM to_jsonb(NOT (flag.ok AND flag.grp)))
""")

# Traversal hides a node when props.hidden is true: a Message that isn't a conversation, or any message in a
# thread that isn't a conversation node. The evidence edges stay; only the display and closeness paths skip it.
SYNC_MESSAGE_HIDDEN = text("""
UPDATE nodes m SET props = m.props || jsonb_build_object('hidden', h.hidden)
FROM (SELECT msg.id, (COALESCE(msg.props->>'item_class', 'conversation') <> 'conversation'
                      OR COALESCE(bool_or(th.props->>'conversational' = 'false'), false)) AS hidden
      FROM nodes msg LEFT JOIN edges it ON it.src = msg.id AND it.type = 'IN_THREAD'
      LEFT JOIN nodes th ON th.id = it.dst AND th.type = 'Thread'
      WHERE msg.type = 'Message' GROUP BY msg.id, msg.props) h
WHERE m.id = h.id AND m.props->'hidden' IS DISTINCT FROM to_jsonb(h.hidden)
""")


def backfill(s: Session, keys: frozenset[str] | None = None) -> int:
    """Classify every item that has no class yet (new, or reset by a content change). Returns rows classified."""
    keys = user_keys(s) if keys is None else keys
    done, last = 0, 0
    while True:
        rows = s.execute(text("""
            SELECT id, source, title, raw_meta, body_text FROM source_items
            WHERE item_class IS NULL AND id > :last ORDER BY id LIMIT :n"""), {"last": last, "n": BATCH}).all()
        if not rows:
            return done
        last = rows[-1].id
        out = [{"id": r.id, "c": classify(r.source, r.title, r.raw_meta, r.body_text, keys)} for r in rows]
        out = [o for o in out if o["c"]]
        if out:
            s.execute(text("UPDATE source_items SET item_class = :c WHERE id = :id"), out)
            done += len(out)
        s.commit()


def apply(s: Session) -> int:
    """Bring classes and their graph mirror up to date. Safe to run any time; cheap when nothing changed."""
    keys = user_keys(s)
    n = backfill(s, keys)
    s.execute(PROMOTE_REPLIED_THREADS)
    s.execute(SYNC_MESSAGE_CLASS)
    s.execute(SYNC_THREAD_FLAG, {"keys": sorted(keys)})
    s.execute(SYNC_MESSAGE_HIDDEN)
    s.commit()
    return n
