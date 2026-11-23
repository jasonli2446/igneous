"""Classify every source item as conversation | event | notification | marketing, from source-native signals only
(no model call, deterministic). Only conversations become Thread/Message nodes that show in traversal; the rest
stay as searchable items and as evidence (a flight confirmation still supports the trip it belongs to).

`classify` is pure: the class of one record, from the record alone. The spine stores it on `source_items.item_class`
in the transaction that builds the record's nodes. `refresh_thread_flags` mirrors it onto the graph, where who you are
also counts: a Message shows as a conversation (`Message.props.item_class`) when its own class is one, when you wrote
it, or when it is a Gmail notification in a thread you answered. A Thread is conversational with two conversation
messages, or one and a message you wrote (`Thread.props.conversational`), and it is shown as a node only when it is
also a group of three or more people. `props.hidden` on both lets traversal skip non-conversations with one predicate.
"""
import re
from collections.abc import Iterable

from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous.graph.identity import OWNER_KEY
from igneous.graph.self_identity import EXTRACTOR as SELF_EXTRACTOR
from igneous.graph.self_identity import is_relay_name, is_service_address

CONVERSATION, EVENT, NOTIFICATION, MARKETING = "conversation", "event", "notification", "marketing"

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


def _classify_email(title: str, meta: dict, body: str) -> str:
    labels = set(meta.get("labels") or [])
    sender = next(iter(meta.get("from") or []), None) or {}
    address = (sender.get("email") or "").lower()
    if labels & {"SENT", "DRAFT"} or (address and address == (meta.get("account") or "").lower()):
        return CONVERSATION  # you wrote it: sent, drafted, or from the account it was read from
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


def classify(source: str, title: str | None, raw_meta: dict | None, body: str | None) -> str | None:
    """The class of one record, from the record alone, or None for sources that aren't communication (files,
    contacts, places). Whether you wrote it is decided on the graph (`refresh_thread_flags`), where every identity
    of yours is known."""
    meta, title, body = raw_meta or {}, title or "", body or ""
    if source in ("gmail", "email"):
        return _classify_email(title, meta, body)
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


# --- the graph mirror: set-based, idempotent ---------------------------------------------------------------------

# You: the owner node, its identity cluster, and the identities the self stage has linked to it since resolve last ran.
OWNER_IDS = f"""
    SELECT me.id FROM nodes me WHERE me.type = 'Person' AND me.key = '{OWNER_KEY}'
    UNION SELECT c.node_id FROM canonical c JOIN nodes me ON me.id = c.canonical_id
          WHERE me.type = 'Person' AND me.key = '{OWNER_KEY}'
    UNION SELECT CASE WHEN l.src = me.id THEN l.dst ELSE l.src END
          FROM nodes me JOIN edges l ON me.id IN (l.src, l.dst) AND l.type = 'SAME_AS' AND l.extractor = '{SELF_EXTRACTOR}'
          WHERE me.type = 'Person' AND me.key = '{OWNER_KEY}'
"""

REFRESH = text(f"""
WITH owner AS MATERIALIZED ({OWNER_IDS}),
scope AS (SELECT id FROM nodes WHERE type = 'Thread'
          AND (CAST(:threads AS bigint[]) IS NULL OR id = ANY(CAST(:threads AS bigint[])))),
member AS MATERIALIZED (
    SELECT it.dst AS thread, it.src AS msg FROM edges it JOIN scope ON scope.id = it.dst
    JOIN nodes m ON m.id = it.src AND m.type = 'Message' WHERE it.type = 'IN_THREAD'),
msg AS MATERIALIZED (
    -- Each message's own class, and whether you wrote it: by the record's own signal (a Gmail SENT or DRAFT label;
    -- GitHub activity in your repositories) or because one of your identities sent it.
    SELECT n.id, si.source, COALESCE(si.item_class, '{CONVERSATION}') AS cls,
           (si.source = 'github' OR (si.source = 'gmail' AND si.raw_meta->'labels' ?| ARRAY['SENT', 'DRAFT'])
            OR EXISTS (SELECT 1 FROM edges snd WHERE snd.dst = n.id AND snd.type = 'SENT'
                       AND snd.src IN (SELECT id FROM owner))) AS mine
    FROM (SELECT DISTINCT msg FROM member) x JOIN nodes n ON n.id = x.msg
    LEFT JOIN source_items si ON si.id = n.source_item_id),
answered AS (SELECT member.thread FROM member JOIN msg ON msg.id = member.msg GROUP BY member.thread
             HAVING bool_or(msg.mine)),
shown AS (
    -- What it shows as: yours is a conversation, and so is a Gmail notification in a thread you answered.
    SELECT msg.id, msg.mine,
           CASE WHEN msg.mine THEN '{CONVERSATION}'
                WHEN msg.cls = '{NOTIFICATION}' AND msg.source = 'gmail'
                     AND EXISTS (SELECT 1 FROM member mb JOIN answered a ON a.thread = mb.thread WHERE mb.msg = msg.id)
                THEN '{CONVERSATION}' ELSE msg.cls END AS cls
    FROM msg),
conv AS (
    SELECT member.thread AS id, count(*) FILTER (WHERE shown.cls = '{CONVERSATION}') AS n,
           bool_or(shown.mine AND shown.cls = '{CONVERSATION}') AS replied
    FROM member JOIN shown ON shown.id = member.msg GROUP BY member.thread),
people AS (
    -- Everyone who wrote or received one of its messages, one per identity.
    SELECT member.thread AS id, count(DISTINCT COALESCE(c.canonical_id, p.id)) AS n
    FROM member CROSS JOIN LATERAL (
        SELECT snd.src AS id FROM edges snd WHERE snd.dst = member.msg AND snd.type = 'SENT'
        UNION SELECT rcv.dst FROM edges rcv WHERE rcv.src = member.msg AND rcv.type = 'RECEIVED') p
    LEFT JOIN canonical c ON c.node_id = p.id
    GROUP BY member.thread),
flag AS (SELECT conv.id, (conv.n >= 2 OR (conv.n >= 1 AND conv.replied)) AS ok, COALESCE(people.n, 0) >= 3 AS grp
         FROM conv LEFT JOIN people USING (id)),
threads AS (
    UPDATE nodes th SET props = th.props || jsonb_build_object('conversational', flag.ok,
                                                               'hidden', NOT (flag.ok AND flag.grp))
    FROM flag WHERE th.id = flag.id
      AND (th.props->'conversational' IS DISTINCT FROM to_jsonb(flag.ok)
           OR th.props->'hidden' IS DISTINCT FROM to_jsonb(NOT (flag.ok AND flag.grp)))
    RETURNING th.id),
hide AS (
    -- A message is hidden when it isn't a conversation, or when a thread it is in isn't one (threads outside this
    -- refresh count as they stand).
    SELECT shown.id, shown.cls,
           shown.cls <> '{CONVERSATION}' OR COALESCE(bool_or(COALESCE(NOT flag.ok,
                                                                     th.props->>'conversational' = 'false')), false)
               AS hidden
    FROM shown JOIN edges it ON it.src = shown.id AND it.type = 'IN_THREAD'
    JOIN nodes th ON th.id = it.dst LEFT JOIN flag ON flag.id = it.dst
    GROUP BY shown.id, shown.cls),
messages AS (
    UPDATE nodes m SET props = m.props || jsonb_build_object('item_class', hide.cls, 'hidden', hide.hidden)
    FROM hide WHERE m.id = hide.id
      AND (m.props->>'item_class' IS DISTINCT FROM hide.cls OR m.props->'hidden' IS DISTINCT FROM to_jsonb(hide.hidden))
    RETURNING m.id)
SELECT (SELECT count(*) FROM threads) + (SELECT count(*) FROM messages)
""")


def refresh_thread_flags(s: Session, thread_ids: Iterable[int] | None = None) -> int:
    """Recompute `conversational` and `hidden` on the given Threads (every Thread when None) and the shown class and
    `hidden` of their Messages, from the graph as it stands: each message's own class, who wrote it (your identities:
    the owner node, its resolved cluster and the self stage's links) and the people in each thread, one per resolved
    identity. Run it after anything those depend on changes: the spine does for the threads each batch touches; after
    resolve, call it with no ids. Writes only rows that change and leaves the commit to the caller. Returns the number
    of nodes updated."""
    ids = None if thread_ids is None else sorted(set(thread_ids))
    if ids == []:
        return 0
    return s.execute(REFRESH, {"threads": ids}).scalar_one()
