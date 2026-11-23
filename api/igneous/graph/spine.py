"""Stage one: the graph read from record metadata (headers, card fields, attendees, file paths). Deterministic, no
model calls.

Which records. A record is pending while `spined_hash` differs from its `content_hash`; a sync that changes a record
clears the marker (migration 0010). `run` builds pending records in id order, one batch per transaction, and writes
back the hash it read: a record that changes while it runs stays pending, and a run that stops leaves every record it
didn't commit pending.

What each record contributed. `spine_node_support` / `spine_edge_support` list the records each node and edge was
built from. Building a record first removes what it built before, so an update replaces exactly its contribution, and
deleting a record deletes its support rows. Whatever loses its last supporting record is queued (database triggers, so
a sync's prune or tombstone counts too) and `settle` deletes it at the end of the run: never the owner, nothing a user
fact holds, and no person or organization another stage still uses. A node several records share (an event series, an
email filed in two folders, a Drive file two accounts see, a person in many messages) stays while any of them remains,
and its `source_item_id` points at one of the records it is. An edge's interval spans what its records observed: a
weekly series' attendance runs from the first occurrence to the last.

Each batch writes every node once, with all its records' names and props merged, and classifies every record
(item_class) in the same transaction. Replies come from the built graph: In-Reply-To names its parent by Message-ID,
a quoted "Original Message" header names its parent by time, subject and sender (quoted.py), and mail with neither
infers one within its thread. Each is recomputed for the messages and threads that changed, and so are the flags of
the threads a batch touched.
"""
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import pairwise

from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous.connectors.mac_contacts import norm_phone
from igneous.graph import dirty, item_class, quoted
from igneous.graph.identity import OWNER_KEY

BATCH = 1000
MAIL = ("email", "gmail")
CONTACTS = ("gcontacts", "maccontacts")

# These edges represent a discrete message or event, rather than an ongoing relationship.
POINT_IN_TIME_TYPES = frozenset({"SENT", "RECEIVED", "IN_THREAD", "REPLIES_TO", "ATTENDED"})
# Extractors of the edges the spine builds from records (each has spine_edge_support rows), and of those it derives
# from the built graph (replies recovered from quotes and inferred within a thread; recomputed, never supported).
RECORD_EXTRACTORS = ("spine", "contacts", "github", "linkedin")
SPINE_EXTRACTORS = (*RECORD_EXTRACTORS, quoted.EXTRACTOR, "spine-inferred")
# Node types only the spine builds. People and organizations are built by other stages too (an extracted mention,
# a resume's employer), so one of those stays while another stage's edge still uses it.
SPINE_TYPES = ("Message", "Thread", "Event", "File", "Folder", "Place")
# Nodes that are one record (copies of one email, one file two accounts see): named by their record's own title.
TITLED = ("Message", "File")


@dataclass
class _Node:
    ord: int
    name: str | None = None      # the best real name this batch's records give it (longest; the first on a tie)
    fallback: str | None = None  # shown until a record gives a real name ("(no subject)", "Drive folder")
    named: str | None = None     # a name a record states for it (a contact card's, a Drive folder's own); last wins
    props: dict = field(default_factory=dict)
    sid: int | None = None       # the first record of this batch the node is
    records: dict[int, bool] = field(default_factory=dict)  # supporting record -> the node is that record


@dataclass
class _Edge:
    ord: int
    confidence: float | None = None
    sid: int | None = None
    records: dict[int, list] = field(default_factory=dict)  # supporting record -> [valid_from, valid_to, the record]


def _least(a, b):
    return b if a is None else a if b is None else min(a, b)


def _greatest(a, b):
    return b if a is None else a if b is None else max(a, b)


def _rows(items: list[dict]) -> str:
    return json.dumps(items, default=str)


class Context:
    """What building a batch needs from outside it: the Message nodes In-Reply-To headers name, the calendar events
    Meet records name, and which contact-card identifiers several people's cards share."""

    def __init__(self, s: Session | None = None, rows=(), cards: dict | None = None):
        self.cards = cards or {}
        self.parents: dict[str, str] = {}
        self.events: dict[str, str] = {}
        if s is None:
            return
        mids = sorted({r.raw_meta.get("in_reply_to") for r in rows if r.source in MAIL and r.raw_meta.get("in_reply_to")})
        self.parents = parent_keys(s, mids)
        eids = sorted({r.raw_meta["event_id"] for r in rows if r.source == "takeout_meet" and r.raw_meta.get("event_id")})
        self.events = event_keys(s, eids)

    def shared(self, identifier: str) -> bool:
        """Whether cards naming two different people both carry this identifier (a family's home phone)."""
        return len({n for n in self.cards.get(identifier, {}).values() if n}) > 1


class Builder:
    """One batch's contributions: nodes merged per (type, key), edges per (src, dst, type, extractor), and the records
    supporting each. `build` adds a record; `flush` writes the batch."""

    def __init__(self, s: Session, context: Context | None = None):
        self.s = s
        self.ctx = context or Context()
        self.item = None
        self.nodes: dict[tuple[str, str], _Node] = {}
        self.refs: set[tuple[str, str]] = set()
        self.edges: dict[tuple, _Edge] = {}
        self.ids: dict[tuple[str, str], int] = {}  # after flush: every node handle's id
        self.new: set[int] = set()                 # after flush: the nodes it created
        self.edge_ids: list[int] = []              # after flush: every edge it wrote

    def build(self, item) -> None:
        """Add one record's contribution (records of a source with no builder contribute nothing)."""
        self.item = item
        builder = BUILDERS.get(item.source)
        if builder:
            builder(self, item)

    def node(self, type_: str, key: str, name: str | None = None, props: dict | None = None, *,
             record: bool = False, fallback: str | None = None, named: str | None = None) -> tuple[str, str]:
        """The node (type, key), supported by the current record. `record`: the node is this record (a message, a
        file, an event occurrence), so its source_item_id may point here."""
        k = (type_, key)
        n = self.nodes.get(k)
        if n is None:
            n = self.nodes[k] = _Node(ord=len(self.nodes))
        rid = self.item.id
        n.records[rid] = n.records.get(rid, False) or record
        if record and n.sid is None:
            n.sid = rid
        if record and name and type_ in TITLED:
            named = name  # a message or file is called what its record calls it now
        if name and name != key and (n.name is None or len(name) > len(n.name)):
            n.name = name
        if fallback and n.fallback is None:
            n.fallback = fallback
        if named:
            n.named = named
        if props:
            n.props.update(props)
        return k

    def ref(self, type_: str, key: str) -> tuple[str, str]:
        """A node this record links to without building it (a reply's parent): the edge is written only if it exists."""
        k = (type_, key)
        if k not in self.nodes:
            self.refs.add(k)
        return k

    def edge(self, src, dst, type_: str, at=None, *, until=..., record: bool = False, extractor: str = "spine",
             confidence: float | None = None) -> None:
        """An edge between two node handles (or node ids), supported by the current record, which observed it from
        `at` until `until` (by default: `at` for point-in-time types, open-ended otherwise). `record`: the edge's
        source_item_id may point at this record."""
        valid_to = (at if type_ in POINT_IN_TIME_TYPES else None) if until is ... else until
        k = (src, dst, type_, extractor)
        e = self.edges.get(k)
        if e is None:
            e = self.edges[k] = _Edge(ord=len(self.edges))
        rid = self.item.id
        seen = e.records.get(rid)
        if seen is None:
            e.records[rid] = [at, valid_to, record]
        else:
            seen[0], seen[1], seen[2] = _least(seen[0], at), _greatest(seen[1], valid_to), seen[2] or record
        if record and e.sid is None:
            e.sid = rid
        if confidence is not None:
            e.confidence = confidence

    # --- writing ------------------------------------------------------------------------------------------------

    def flush(self) -> None:
        s = self.s
        handles = list(self.nodes) + sorted(self.refs - set(self.nodes))
        ids = _node_ids(s, handles)
        fresh = [k for k in self.nodes if k not in ids]
        if fresh:
            for i, t, k in s.execute(INSERT_NODES, {"rows": _rows([
                    {"ord": self.nodes[h].ord, "type": h[0], "key": h[1], "name": self.nodes[h].name,
                     "named": self.nodes[h].named, "fallback": self.nodes[h].fallback, "props": self.nodes[h].props,
                     "sid": self.nodes[h].sid} for h in fresh])}):
                ids[(t, k)] = i
                self.new.add(i)
            ids.update(_node_ids(s, [h for h in fresh if h not in ids]))  # built meanwhile by another run
        known = [h for h in self.nodes if h in ids and ids[h] not in self.new]
        if known:
            s.execute(UPDATE_NODES, {"rows": _rows([
                {"id": ids[h], "name": self.nodes[h].name, "named": self.nodes[h].named,
                 "fallback": self.nodes[h].fallback, "props": self.nodes[h].props, "sid": self.nodes[h].sid}
                for h in known])})
        self.ids = ids
        support = [(ids[h], rid, rep) for h, n in self.nodes.items() if h in ids for rid, rep in n.records.items()]
        if support:
            n_, r_, x_ = zip(*support, strict=True)
            s.execute(text("""
                INSERT INTO spine_node_support (node_id, source_item_id, represents)
                SELECT * FROM unnest(CAST(:n AS bigint[]), CAST(:r AS bigint[]), CAST(:x AS boolean[]))
                ON CONFLICT (node_id, source_item_id) DO UPDATE
                    SET represents = spine_node_support.represents OR EXCLUDED.represents
            """), {"n": list(n_), "r": list(r_), "x": list(x_)})
        self._flush_edges()

    def _flush_edges(self) -> None:
        """Write the batch's edges: new ones with their interval, and every edge's supporting records. An edge that
        existed before gets its interval recomputed from all its support."""
        s = self.s
        merged: dict[tuple, _Edge] = {}
        for (src, dst, type_, ex), e in sorted(self.edges.items(), key=lambda kv: kv[1].ord):
            a = src if isinstance(src, int) else self.ids.get(src)
            b = dst if isinstance(dst, int) else self.ids.get(dst)
            if a is None or b is None or a == b:
                continue  # a reply to a message that isn't built, or an identity linked to itself
            if type_ == "SAME_AS":
                a, b = min(a, b), max(a, b)  # one undirected edge per pair
            k = (a, b, type_, ex)
            if k not in merged:
                merged[k] = _Edge(ord=len(merged), confidence=e.confidence, sid=e.sid)
            m = merged[k]
            m.sid = m.sid or e.sid
            for rid, (f, t, rep) in e.records.items():
                seen = m.records.get(rid)
                m.records[rid] = [f, t, rep] if seen is None else [_least(seen[0], f), _greatest(seen[1], t),
                                                                   seen[2] or rep]
        if not merged:
            return
        keys = list(merged)
        # An edge with an endpoint this flush created can't exist yet.
        maybe = [k for k in keys if k[0] not in self.new and k[1] not in self.new]
        found = _edge_ids(s, maybe)
        old = {found[k][0] for k in found}
        fresh = [k for k in keys if k not in found]
        if fresh:
            span_of = {k: (min((f for f, _t, _x in merged[k].records.values() if f is not None), default=None),
                           max((t for _f, t, _x in merged[k].records.values() if t is not None), default=None))
                       for k in fresh}
            for r in s.execute(text("""
                INSERT INTO edges (src, dst, type, extractor, confidence, source_item_id, valid_from, valid_to)
                SELECT a, b, t, x, c, sid, f, u
                FROM unnest(CAST(:o AS int[]), CAST(:a AS bigint[]), CAST(:b AS bigint[]), CAST(:t AS text[]),
                            CAST(:x AS text[]), CAST(:c AS real[]), CAST(:sid AS bigint[]), CAST(:f AS timestamptz[]),
                            CAST(:u AS timestamptz[])) AS r(o, a, b, t, x, c, sid, f, u)
                ORDER BY o
                ON CONFLICT (src, dst, type, extractor) DO NOTHING
                RETURNING id, src, dst, type, extractor, source_item_id
            """), {"o": [merged[k].ord for k in fresh], "a": [k[0] for k in fresh], "b": [k[1] for k in fresh],
                   "t": [k[2] for k in fresh], "x": [k[3] for k in fresh], "c": [merged[k].confidence for k in fresh],
                   "sid": [merged[k].sid for k in fresh], "f": [span_of[k][0] for k in fresh],
                   "u": [span_of[k][1] for k in fresh]}):
                found[(r.src, r.dst, r.type, r.extractor)] = (r.id, r.source_item_id)
            raced = _edge_ids(s, [k for k in fresh if k not in found])  # written meanwhile by another run
            found.update(raced)
            old |= {i for i, _sid in raced.values()}
        pointless = [(found[k][0], merged[k].sid) for k in keys
                     if k in found and found[k][1] is None and merged[k].sid is not None]
        if pointless:  # an edge first built without a record of its own now has one
            e_, sid_ = zip(*pointless, strict=True)
            s.execute(text("""
                UPDATE edges e SET source_item_id = r.sid
                FROM unnest(CAST(:e AS bigint[]), CAST(:sid AS bigint[])) AS r(id, sid)
                WHERE e.id = r.id AND e.source_item_id IS NULL
            """), {"e": list(e_), "sid": list(sid_)})
        support = [(found[k][0], rid, rep, f, t) for k in keys if k in found
                   for rid, (f, t, rep) in merged[k].records.items()]
        e_, r_, x_, f_, t_ = zip(*support, strict=True)
        s.execute(text("""
            INSERT INTO spine_edge_support (edge_id, source_item_id, represents, valid_from, valid_to)
            SELECT * FROM unnest(CAST(:e AS bigint[]), CAST(:r AS bigint[]), CAST(:x AS boolean[]),
                                 CAST(:f AS timestamptz[]), CAST(:t AS timestamptz[]))
            ON CONFLICT (edge_id, source_item_id) DO UPDATE SET
                represents = spine_edge_support.represents OR EXCLUDED.represents,
                valid_from = LEAST(spine_edge_support.valid_from, EXCLUDED.valid_from),
                valid_to = GREATEST(spine_edge_support.valid_to, EXCLUDED.valid_to)
        """), {"e": list(e_), "r": list(r_), "x": list(x_), "f": list(f_), "t": list(t_)})
        self.edge_ids = sorted({found[k][0] for k in keys if k in found})
        span(s, sorted(old))


def _edge_ids(s: Session, keys: list[tuple]) -> dict[tuple, tuple[int, int | None]]:
    """(src, dst, type, extractor) -> (edge id, source_item_id) for the edges that exist."""
    if not keys:
        return {}
    return {(r.src, r.dst, r.type, r.extractor): (r.id, r.source_item_id) for r in s.execute(text("""
        SELECT e.id, e.src, e.dst, e.type, e.extractor, e.source_item_id
        FROM unnest(CAST(:a AS bigint[]), CAST(:b AS bigint[]), CAST(:t AS text[]), CAST(:x AS text[])) AS k(a, b, t, x)
        JOIN edges e ON e.src = k.a AND e.dst = k.b AND e.type = k.t AND e.extractor = k.x
    """), {"a": [k[0] for k in keys], "b": [k[1] for k in keys], "t": [k[2] for k in keys],
           "x": [k[3] for k in keys]})}


def _node_ids(s: Session, handles: list[tuple[str, str]]) -> dict[tuple[str, str], int]:
    if not handles:
        return {}
    return {(t, k): i for i, t, k in s.execute(text("""
        SELECT n.id, n.type, n.key FROM unnest(CAST(:t AS text[]), CAST(:k AS text[])) AS h(type, key)
        JOIN nodes n ON n.type = h.type AND n.key = h.key
    """), {"t": [h[0] for h in handles], "k": [h[1] for h in handles]})}


INSERT_NODES = text("""
INSERT INTO nodes (type, key, name, props, source_item_id)
SELECT r.type, r.key, COALESCE(r.named, r.name, r.fallback, r.key), COALESCE(r.props, '{}'), r.sid
FROM jsonb_to_recordset(CAST(:rows AS jsonb)) AS r(ord int, type text, key text, name text, named text, fallback text,
                                                    props jsonb, sid bigint)
ORDER BY r.ord
ON CONFLICT (type, key) DO NOTHING
RETURNING id, type, key
""")

# A node's name: your own rename stands; then a name a record states for it (a contact card's, a Drive folder's own);
# a card's name is not replaced by anything less; otherwise a real name replaces the key or a placeholder, and a
# longer name a shorter one ("J. Doe" -> "Jane Doe"). Props merge, later records winning per key. source_item_id keeps
# the record it points at (settle moves it when that record stops being this node).
UPDATE_NODES = text("""
UPDATE nodes n SET name = v.name, props = v.props, source_item_id = v.sid
FROM (
    SELECT n.id,
           CASE WHEN n.props->>'renamed' = 'true' THEN n.name
                WHEN r.named IS NOT NULL THEN r.named
                WHEN n.props->>'named_by' = 'contacts' OR r.name IS NULL THEN n.name
                WHEN n.name = n.key OR n.name = r.fallback THEN r.name
                WHEN length(r.name) > length(n.name) THEN r.name ELSE n.name END AS name,
           n.props || COALESCE(r.props, '{}') AS props,
           COALESCE(n.source_item_id, r.sid) AS sid
    FROM jsonb_to_recordset(CAST(:rows AS jsonb)) AS r(id bigint, name text, named text, fallback text, props jsonb,
                                                        sid bigint)
    JOIN nodes n ON n.id = r.id) v
WHERE n.id = v.id AND (n.name, n.props, n.source_item_id) IS DISTINCT FROM (v.name, v.props, v.sid)
""")


def span(s: Session, edge_ids: list[int]) -> None:
    """Set each edge's interval to span what its supporting records observed."""
    if edge_ids:
        s.execute(text("""
            UPDATE edges e SET valid_from = x.f, valid_to = x.t
            FROM (SELECT edge_id, min(valid_from) AS f, max(valid_to) AS t FROM spine_edge_support
                  WHERE edge_id = ANY(:ids) GROUP BY edge_id) x
            WHERE e.id = x.edge_id AND (e.valid_from IS DISTINCT FROM x.f OR e.valid_to IS DISTINCT FROM x.t)
        """), {"ids": edge_ids})


# --- what each kind of record builds ------------------------------------------------------------------------------

def _name_from(addr: dict, x_from: str | None) -> str:
    if addr.get("name") and "@" not in addr["name"]:
        return addr["name"]
    if x_from:
        clean = re.sub(r"<.*?>|\(.*?\)|\".*?\"", "", x_from).strip(" ,")
        if clean and "@" not in clean:
            return clean
    local = addr["email"].split("@")[0]
    return " ".join(w.capitalize() for w in re.split(r"[._]", local) if w)


REPLY_PREFIX = re.compile(r"^\s*((re|fw|fwd)\s*:\s*)+", re.I)


def _thread_key(meta: dict, title: str) -> str:
    """Root of the reply chain if headers give one. Otherwise (all of Enron) normalized subject plus the
    participant set, so unrelated messages that share a generic subject ("Thank You") don't merge."""
    if meta.get("gmail_thread_id"):
        return f"gmail:{meta['gmail_thread_id']}"
    if meta.get("references"):
        return meta["references"][0]
    if meta.get("in_reply_to"):
        return meta["in_reply_to"]
    subj = REPLY_PREFIX.sub("", title or "").strip().lower()
    if not subj:
        return f"solo:{meta.get('fingerprint') or meta.get('message_id')}"
    people = sorted({a["email"] for f in ("from", "to", "cc") for a in meta.get(f, [])})
    # Mass mail (petitions, announcements) is one thread regardless of who sent each copy.
    if len(people) > 12:
        return f"subject:{subj}"
    return f"subject:{subj}|{','.join(people)}"


def message_key(meta: dict, item_id: int) -> str:
    """The Message node a mail record builds: its content fingerprint, so copies of one email (Enron files one in
    several folders, each with its own Message-ID; Gmail has one message in two accounts) are one node."""
    return meta.get("fingerprint") or meta.get("message_id") or f"item:{item_id}"


def parent_keys(s: Session, message_ids: list[str]) -> dict[str, str]:
    """Message-ID -> the Message node key of the record carrying it (the first ingested, if several do)."""
    if not message_ids:
        return {}
    out: dict[str, str] = {}
    for mid, rid, fingerprint in s.execute(text("""
        SELECT raw_meta->>'message_id', id, raw_meta->>'fingerprint' FROM source_items
        WHERE source IN ('email', 'gmail') AND raw_meta->>'message_id' = ANY(:m) ORDER BY id
    """), {"m": message_ids}):
        out.setdefault(mid, message_key({"fingerprint": fingerprint, "message_id": mid}, rid))
    return out


def _event_key(source: str, external_id: str, meta: dict) -> str:
    return meta.get("event_key") or f"{source}:{meta.get('series_key') or external_id}"


def event_keys(s: Session, event_ids: list[str]) -> dict[str, str]:
    """Calendar event id -> the Event node key of the calendar record for it (gcal external ids end in the event id)."""
    if not event_ids:
        return {}
    out: dict[str, str] = {}
    for eid, external_id, meta in s.execute(text("""
        SELECT regexp_replace(external_id, '^.*/', ''), external_id, raw_meta FROM source_items
        WHERE source = 'gcal' AND regexp_replace(external_id, '^.*/', '') = ANY(:e) ORDER BY id
    """), {"e": event_ids}):
        out.setdefault(eid, _event_key("gcal", external_id, meta))
    return out


def build_email(b: Builder, item) -> None:
    meta = item.raw_meta
    key = message_key(meta, item.id)
    mid = b.node("Message", key, item.title, {"source": item.source}, record=True, fallback="(no subject)")
    for addr in meta.get("from", [])[:1]:
        sender = b.node("Person", addr["email"], _name_from(addr, meta.get("x_from")), {"email": addr["email"]})
        b.edge(sender, mid, "SENT", item.occurred_at, record=True)
    for field_ in ("to", "cc", "bcc"):
        for addr in meta.get(field_, []):
            recipient = b.node("Person", addr["email"], _name_from(addr, None), {"email": addr["email"]})
            b.edge(mid, recipient, "RECEIVED", item.occurred_at, record=True)
    tid = b.node("Thread", _thread_key(meta, item.title), REPLY_PREFIX.sub("", item.title or "").strip() or None,
                 fallback="(no subject)")
    b.edge(mid, tid, "IN_THREAD", item.occurred_at, record=True)
    parent = b.ctx.parents.get(meta.get("in_reply_to"))
    if parent and parent != key:  # the message it answers, once that one is built
        b.edge(mid, b.ref("Message", parent), "REPLIES_TO", item.occurred_at, record=True)


def build_chat(b: Builder, item) -> None:
    """A conversation-day from Messages: one Message node for the day, a Thread per chat, a Person per handle.
    Phone numbers and Apple IDs are the keys; entity resolution can later join them to email identities."""
    meta = item.raw_meta
    mid = b.node("Message", f"imessage:{item.external_id}", item.title, {"source": "imessage"}, record=True,
                 fallback="(chat)")
    # Group chats are marked so the graph can treat them as groups ("who else is in the Gloomhaven Night chat").
    tid = b.node("Thread", f"imessage-chat:{meta.get('chat')}", (item.title or "").rsplit(" — ", 1)[0] or None,
                 {"source": "imessage", "group": bool(meta.get("group")) or len(meta.get("participants", [])) > 1},
                 fallback="chat")
    b.edge(mid, tid, "IN_THREAD", item.occurred_at, record=True)
    me = b.node("Person", OWNER_KEY, None, {"self": True}, fallback="Me")
    senders = set(meta.get("senders", []))
    members = {h.lower(): h for h in meta.get("participants", [])}
    # Everyone in the chat received the day's conversation, and whoever spoke also SENT it, so two members who both
    # spoke are correspondents.
    for key, handle in members.items():
        if "@" not in key:
            key = norm_phone(key) or key  # '+16145551234' / '6145551234' -> one key, matching contact cards
        pid = b.node("Person", key, handle, {"handle": handle})
        b.edge(mid, pid, "RECEIVED", item.occurred_at, record=True)
        if handle in senders:
            b.edge(pid, mid, "SENT", item.occurred_at, record=True)
    b.edge(mid, me, "RECEIVED", item.occurred_at, record=True)
    if "me" in senders:
        b.edge(me, mid, "SENT", item.occurred_at, record=True)


def build_ai_chat(b: Builder, item) -> None:
    """One AI session-day: a Message from you in a Thread for the session. The repo it ran in becomes a
    Folder-like anchor (key `repo:<name>`) that the entity stage can join to the matching GitHub repo, local
    folder, or project, which is how an assistant conversation lands in the right project."""
    meta = item.raw_meta
    tool = meta.get("tool", "ai")
    mid = b.node("Message", f"ai:{item.external_id}", item.title, {"source": "ai_chats", "tool": tool}, record=True,
                 fallback="(AI session)")
    tid = b.node("Thread", f"ai-session:{tool}/{meta.get('session')}", (item.title or "").rsplit(" (", 1)[0] or None,
                 {"source": "ai_chats", "tool": tool}, fallback="AI session")
    b.edge(mid, tid, "IN_THREAD", item.occurred_at, record=True)
    me = b.node("Person", OWNER_KEY, None, {"self": True}, fallback="Me")
    b.edge(me, mid, "SENT", item.occurred_at, record=True)
    if meta.get("repo"):
        repo = b.node("Folder", f"repo:{meta['repo'].lower()}", meta["repo"], {"path": meta.get("cwd"), "kind": "repo"})
        b.edge(repo, tid, "CONTAINS", item.occurred_at, record=True)


def build_event(b: Builder, item) -> None:
    """Calendar event: one Event node per recurring series (a weekly class is one node, not 15), so attendees'
    ATTENDED edges dedupe to one per series, spanning the occurrences each attended. Each occurrence stays its own
    source item, which is what relationship strength counts. Organizer and attendees ATTENDED it (decliners
    excluded)."""
    meta = item.raw_meta
    eid = b.node("Event", _event_key(item.source, item.external_id, meta), item.title,
                 {"series": bool(meta.get("recurring"))}, record=True, fallback="(event)")
    people = list(meta.get("attendees", []))
    if meta.get("organizer", {}).get("email"):
        people.append({**meta["organizer"], "response": "accepted"})
    for a in people:
        if a.get("response") == "declined" or "@" not in a.get("email", ""):
            continue
        pid = b.node("Person", a["email"], _name_from(a, None), {"email": a["email"]})
        b.edge(pid, eid, "ATTENDED", item.occurred_at, record=True)


def card_keys(meta: dict) -> list[str]:
    """A contact card's identifiers as Person keys: addresses lowercased, phone numbers normalized."""
    keys = [e.strip().lower() for e in meta.get("emails", []) if e and e.strip()]
    keys += [norm_phone(p) or p for p in meta.get("phones", []) if p]
    return list(dict.fromkeys(keys))


def card_name(title: str | None, keys: list[str]) -> str | None:
    """The person a card names, or None for a nameless card (its title is only one of its identifiers)."""
    name = (title or "").strip()
    if not name or "@" in name or name.lower() in keys or norm_phone(name) in keys:
        return None
    return name


def contact_cards(s: Session) -> dict[str, dict[int, str | None]]:
    """Each identifier on a contact card -> the cards carrying it, with the name each card gives (None if nameless)."""
    out: dict[str, dict[int, str | None]] = defaultdict(dict)
    for rid, title, meta in s.execute(text("SELECT id, title, raw_meta FROM source_items WHERE source = ANY(:c)"),
                                      {"c": list(CONTACTS)}):
        keys = card_keys(meta or {})
        name = card_name(title, keys)
        for k in keys:
            out[k][rid] = " ".join(name.lower().split()) if name else None
    return dict(out)


def build_contact(b: Builder, item) -> None:
    """A contact card names one human with several addresses and numbers. It names the matching Person nodes
    (so a Messages handle shows as "Jane Doe", not "+16145551234") and emits SAME_AS between every identity on the
    card: ground truth for entity resolution (extractor 'contacts', which `resolve --undo` leaves alone). An
    identifier that cards naming different people share (a family's home phone) belongs to none of them: the card
    neither names it nor links it."""
    keys = card_keys(item.raw_meta)
    name = card_name(item.title, keys)
    linked = []
    for k in keys:
        props = {"handle": k} if "@" not in k else {"email": k}
        if b.ctx.shared(k):
            b.node("Person", k, None, {**props, "named_by": None}, named=k)
            continue
        pid = b.node("Person", k, None, {**props, "named_by": "contacts"} if name else props, named=name)
        linked.append(pid)
    for a, c in pairwise(linked):
        b.edge(a, c, "SAME_AS", record=True, extractor="contacts", confidence=1.0)


def build_github(b: Builder, item) -> None:
    """Represent GitHub activity as authored messages anchored to a repository.

    GitHub logins are stable person keys. A verified commit email is a separate identity
    and receives a trusted SAME_AS edge, rather than guessing from display names.
    """
    meta = item.raw_meta
    repo_name = meta.get("repo", "github")
    repo = b.node("Folder", f"repo:{repo_name.lower()}", repo_name, {"kind": "repo", "source": "github"})
    mid = b.node("Message", f"github:{item.external_id}", item.title or repo_name, {"source": "github"}, record=True)
    b.edge(repo, mid, "CONTAINS", item.occurred_at, record=True)
    thread = b.node("Thread", f"github:{repo_name}:{meta.get('kind')}:{meta.get('number', meta.get('day', item.external_id))}",
                    item.title or repo_name, {"source": "github"})
    b.edge(mid, thread, "IN_THREAD", item.occurred_at, record=True)
    identities = [meta.get("author", {}), *meta.get("commits", [])]
    for identity in identities:
        login, email = identity.get("login"), identity.get("email")
        if not isinstance(login, str) or not login.strip() or "/" in login:
            continue
        pid = b.node("Person", f"github:{login.lower()}", login, {"github_login": login})
        b.edge(pid, mid, "SENT", item.occurred_at, record=True)
        if email and email.lower() != "notifications@github.com":
            email_id = b.node("Person", email.lower(), None, {"email": email.lower()})
            b.edge(pid, email_id, "SAME_AS", record=True, extractor="github", confidence=1.0)
    for comment in meta.get("comments", []):
        login = comment.get("login")
        if isinstance(login, str) and login.strip() and "/" not in login:
            pid = b.node("Person", f"github:{login.lower()}", login, {"github_login": login})
            b.edge(pid, mid, "SENT", item.occurred_at, record=True)


def build_linkedin(b: Builder, item) -> None:
    """Your LinkedIn export. The profile item is you: it names the owner node, and graph/profile.py reads its
    positions and schools. A connection names one human, keyed by the connection's email when the export has one
    (so it merges with mail identities by key), else by `linkedin:<profile slug>`; either way named by LinkedIn
    (props.named_by='linkedin') and MEMBER_OF its company, keyed by the company's own name (`org:<slug>`, as your
    resume's employers are), never by the connection's address. With an email, the profile identity
    `linkedin:<slug>` is joined to the address by a trusted SAME_AS."""
    from igneous.connectors.linkedin import profile_slug

    meta = item.raw_meta
    if meta.get("kind") == "profile":
        b.node("Person", OWNER_KEY, meta.get("person_name") or None, {"self": True}, fallback="Me")
        return
    if meta.get("kind") != "connection" or not meta.get("name"):
        return
    name, email = meta["name"], (meta.get("email") or "").lower() or None
    slug = profile_slug(meta.get("url") or "")
    props = {"named_by": "linkedin", "linkedin_url": meta.get("url"), "position": meta.get("position") or None}
    props = {k: v for k, v in props.items() if v}
    key = email or (f"linkedin:{slug}" if slug else f"linkedin:{item.external_id.split('/', 1)[-1]}")
    pid = b.node("Person", key, name, {**props, **({"email": email} if email else {})}, record=True)
    if email and slug:
        lid = b.node("Person", f"linkedin:{slug}", name, props, record=True)
        b.edge(lid, pid, "SAME_AS", record=True, extractor="linkedin", confidence=1.0)
    company = (meta.get("company") or "").strip()
    org_slug = re.sub(r"[^a-z0-9]+", "-", company.lower()).strip("-")
    if len(org_slug) >= 2:
        oid = b.node("Organization", f"org:{org_slug}", company, {"kind": "employer", "source": "linkedin"})
        b.edge(pid, oid, "MEMBER_OF", record=True, extractor="linkedin", confidence=1.0)


def build_file(b: Builder, item) -> None:
    path = item.raw_meta.get("path", item.external_id)
    folders = item.raw_meta.get("folders")
    if not isinstance(path, str):
        path = item.external_id
    if not isinstance(folders, list) or not all(isinstance(folder, str) for folder in folders):
        parent = path.rsplit("/", 1)[0] if "/" in path else ""
        folders = []
        while parent:
            folders.append(parent)
            parent = parent.rsplit("/", 1)[0] if "/" in parent else ""
        folders.reverse()
    # A Drive file shared with two connected accounts is one file: key on its Drive id, not the account.
    drive_id = item.raw_meta.get("drive_id") if item.source == "drive" else None
    fkey = f"drive:{drive_id}" if drive_id else f"{item.source}:{item.external_id}"
    fid = b.node("File", fkey, item.title, {"source": item.source, "path": path}, record=True,
                 fallback=item.external_id)
    # Drive preserves every parent ID in metadata: a file can belong to more than one folder,
    # which cannot be represented faithfully by a single filesystem-style path.
    parents = item.raw_meta.get("parents")
    if isinstance(parents, list) and all(isinstance(parent, str) for parent in parents):
        names = item.raw_meta.get("parent_names") or {}
        for parent in parents:
            # The folder's own name, when Drive gave one, replaces the placeholder.
            folder = b.node("Folder", f"{item.source}:drive/{parent}", None, {"path": f"drive/{parent}"},
                            fallback="Drive folder", named=names.get(parent) or None)
            b.edge(folder, fid, "CONTAINS")
        return
    child = fid
    for parent in reversed(folders):
        folder = b.node("Folder", f"{item.source}:{parent}", parent.rsplit("/", 1)[-1], {"path": parent})
        b.edge(folder, child, "CONTAINS")
        child = folder
    # A discovered .git remote is a deterministic, source-backed hard link from its on-disk folder to the
    # repository anchor shared with coding-session and GitHub evidence. No model is needed to make this join.
    if item.raw_meta.get("kind") == "repo" and isinstance(item.raw_meta.get("repo"), str):
        repo_name = item.raw_meta["repo"]
        repo = b.node("Folder", f"repo:{repo_name.lower()}", repo_name,
                      {"path": path, "kind": "repo", "remote": item.raw_meta.get("remote")})
        b.edge(repo, child, "CONTAINS", item.occurred_at, record=True)


def build_takeout_chat(b: Builder, item) -> None:
    """A Google Chat conversation-day, shaped like an iMessage one: a Message in a Thread per conversation,
    People keyed by email. Whoever spoke SENT the day; everyone in the conversation RECEIVED it."""
    meta = item.raw_meta
    mid = b.node("Message", f"takeout_chat:{item.external_id}", item.title, {"source": "takeout_chat"}, record=True,
                 fallback="(chat)")
    tid = b.node("Thread", f"takeout-chat:{meta.get('group')}", f"Google Chat {meta.get('group', '')}".strip(),
                 {"source": "takeout_chat", "group": len(meta.get("participants", [])) > 2})
    b.edge(mid, tid, "IN_THREAD", item.occurred_at, record=True)
    senders = set(meta.get("senders", []))
    for email in meta.get("participants", []):
        pid = b.node("Person", email, None, {"email": email})
        b.edge(mid, pid, "RECEIVED", item.occurred_at, record=True)
        if email in senders:
            b.edge(pid, mid, "SENT", item.occurred_at, record=True)


def _when(value) -> datetime | None:
    """An ISO timestamp from record metadata, in UTC when it names no zone; None when it isn't one."""
    try:
        when = value if isinstance(value, datetime) else datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None
    return when if when is None or when.tzinfo else when.replace(tzinfo=UTC)


def build_takeout(b: Builder, item) -> None:
    """Google Takeout day-items. Each item is a File node (so it is chunked, searchable and linkable to a
    Project by phrase match). Maps items also create Place nodes: you VISITED a place (one edge per place,
    spanning first to last visit; counts come from the item metadata) or SAVED it (starred, listed, reviewed)."""
    meta = item.raw_meta
    if item.source == "takeout_chat":
        return build_takeout_chat(b, item)
    fid = b.node("File", f"{item.source}:{item.external_id}", item.title,
                 {"source": item.source, "kind": meta.get("kind") or meta.get("product", "activity")}, record=True,
                 fallback=item.external_id)
    if item.source == "takeout_meet" and (event := b.ctx.events.get(meta.get("event_id"))):
        # Meet records the Calendar event id; gcal external ids end in it ("<account>/<calendar>/<event id>").
        b.edge(fid, b.ref("Event", event), "RELATED_TO", item.occurred_at, until=item.occurred_at, record=True)
    if item.source != "takeout_maps":
        return
    me = b.node("Person", OWNER_KEY, None, {"self": True}, fallback="Me")
    visited = meta.get("kind") == "visits"
    for v in meta.get("visits" if visited else "places", []):
        props = {k: v[k] for k in ("address", "lat", "lng", "place_id") if v.get(k) is not None}
        pid = b.node("Place", v["key"], v.get("name") or v.get("address"), props, fallback="Unnamed place")
        start = _when(v.get("start")) or item.occurred_at
        b.edge(me, pid, "VISITED" if visited else "SAVED", start, until=_when(v.get("end")) or start, record=True)


# Every value of source_items.source (source_catalog.py) and what builds it. A value missing here would be marked
# built without contributing anything; check-graph reports values the catalog doesn't know.
BUILDERS = {
    "email": build_email, "gmail": build_email,
    "imessage": build_chat,
    "ai_chats": build_ai_chat,
    "github": build_github,
    "gcal": build_event,
    "gcontacts": build_contact, "maccontacts": build_contact,
    "linkedin": build_linkedin,
    "fs": build_file, "drive": build_file, "notes": build_file,
    "takeout_chat": build_takeout, "takeout_meet": build_takeout, "takeout_tasks": build_takeout,
    "takeout_activity": build_takeout, "takeout_chrome": build_takeout, "takeout_maps": build_takeout,
}


# --- replies -------------------------------------------------------------------------------------------------------

# Within a thread, a "Re:" message with no reply header and no quoted parent answers the latest strictly earlier
# message from a different sender. Window functions, per thread: `last_before` is the last message before the reply;
# if the reply's own sender wrote it, the parent is the message just before that sender's run of messages.
INFER_REPLIES = text("""
WITH m AS MATERIALIZED (
    SELECT it.dst AS thread, it.src AS id, it.valid_from AS at, msg.name AS title,
           (SELECT min(snd.src) FROM edges snd WHERE snd.dst = it.src AND snd.type = 'SENT') AS sender,
           EXISTS (SELECT 1 FROM edges r WHERE r.src = it.src AND r.type = 'REPLIES_TO'
                   AND r.extractor <> 'spine-inferred') AS has_parent
    FROM edges it JOIN nodes msg ON msg.id = it.src AND msg.type = 'Message'
    WHERE it.type = 'IN_THREAD' AND it.dst = ANY(CAST(:threads AS bigint[])) AND it.valid_from IS NOT NULL),
o AS (
    SELECT m.*, row_number() OVER w AS idx,
           CASE WHEN sender IS DISTINCT FROM lag(sender) OVER w THEN 1 ELSE 0 END AS starts_run
    FROM m WINDOW w AS (PARTITION BY thread ORDER BY at, id)),
r AS (
    SELECT o.*, sum(starts_run) OVER (PARTITION BY thread ORDER BY at, id) AS run,
           max(idx) OVER (PARTITION BY thread ORDER BY at
                          RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW EXCLUDE GROUP) AS last_before
    FROM o),
runs AS (SELECT thread, run, min(idx) AS first FROM r GROUP BY thread, run)
SELECT child.id AS child, child.at,
       CASE WHEN p.sender IS DISTINCT FROM child.sender THEN p.id ELSE q.id END AS parent
FROM r child
JOIN r p ON p.thread = child.thread AND p.idx = child.last_before
JOIN runs ON runs.thread = p.thread AND runs.run = p.run
LEFT JOIN r q ON q.thread = child.thread AND q.idx = runs.first - 1
WHERE child.title ~* '^\\s*re\\s*:' AND NOT child.has_parent
""")


def infer_replies(s: Session, threads) -> int:
    """Recompute the inferred replies of these threads (and of the other threads their messages are in), replacing
    the previous ones. Returns the number of edges written or removed."""
    ids = sorted(set(threads))
    if not ids:
        return 0
    ids = sorted(set(s.execute(text("""
        SELECT DISTINCT b.dst FROM edges a JOIN edges b ON b.src = a.src AND b.type = 'IN_THREAD'
        WHERE a.type = 'IN_THREAD' AND a.dst = ANY(:t)
    """), {"t": ids}).scalars()) | set(ids))
    want = {(r.child, r.parent): r.at for r in s.execute(INFER_REPLIES, {"threads": ids}) if r.parent is not None}
    have = set(s.execute(text("""
        SELECT DISTINCT r.src, r.dst FROM edges r JOIN edges it ON it.src = r.src AND it.type = 'IN_THREAD'
        WHERE it.dst = ANY(:t) AND r.type = 'REPLIES_TO' AND r.extractor = 'spine-inferred'
    """), {"t": ids}).all())
    stale = sorted(have - set(want))
    if stale:
        s.execute(text("""
            DELETE FROM edges e USING unnest(CAST(:a AS bigint[]), CAST(:b AS bigint[])) AS x(src, dst)
            WHERE e.src = x.src AND e.dst = x.dst AND e.type = 'REPLIES_TO' AND e.extractor = 'spine-inferred'
        """), {"a": [p[0] for p in stale], "b": [p[1] for p in stale]})
    fresh = sorted(set(want) - have)
    if fresh:
        s.execute(text("""
            INSERT INTO edges (src, dst, type, valid_from, valid_to, extractor)
            VALUES (:c, :p, 'REPLIES_TO', :at, :at, 'spine-inferred')
            ON CONFLICT (src, dst, type, extractor) DO NOTHING
        """), [{"c": c, "p": p, "at": want[(c, p)]} for c, p in fresh])
    return len(stale) + len(fresh)


def _threads_of(s: Session, messages) -> set[int]:
    ids = sorted(set(messages))
    if not ids:
        return set()
    return set(s.execute(text("SELECT DISTINCT dst FROM edges WHERE src = ANY(:m) AND type = 'IN_THREAD'"),
                         {"m": ids}).scalars())


def _link_late_parents(s: Session, rows, batch: list[int], cards: dict) -> set[int]:
    """Records built earlier that name one of this batch's records: replies whose In-Reply-To names a message built
    now, and Meet records naming an event built now. Their links become part of their own contribution. Returns
    the replies' Message nodes."""
    mids = sorted({r.raw_meta["message_id"] for r in rows if r.source in MAIL and r.raw_meta.get("message_id")})
    eids = sorted({r.external_id.rsplit("/", 1)[-1] for r in rows if r.source == "gcal"})
    late = []
    if mids:
        late += s.execute(text("""
            SELECT id, source, external_id, title, raw_meta, occurred_at FROM source_items
            WHERE source IN ('email', 'gmail') AND raw_meta->>'in_reply_to' IS NOT NULL
              AND raw_meta->>'in_reply_to' = ANY(:m) AND spined_hash = content_hash AND id <> ALL(:batch)
        """), {"m": mids, "batch": batch}).all()
    if eids:
        late += s.execute(text("""
            SELECT id, source, external_id, title, raw_meta, occurred_at FROM source_items
            WHERE source = 'takeout_meet' AND raw_meta->>'event_id' = ANY(:e)
              AND spined_hash = content_hash AND id <> ALL(:batch)
        """), {"e": eids, "batch": batch}).all()
    if not late:
        return set()
    b = Builder(s, Context(s, late, cards))
    for item in late:
        b.item = item
        meta = item.raw_meta
        if item.source == "takeout_meet":
            event = b.ctx.events.get(meta.get("event_id"))
            if event:
                b.edge(b.ref("File", f"{item.source}:{item.external_id}"), b.ref("Event", event), "RELATED_TO",
                       item.occurred_at, until=item.occurred_at, record=True)
            continue
        key, parent = message_key(meta, item.id), b.ctx.parents.get(meta.get("in_reply_to"))
        if parent and parent != key:
            b.edge(b.ref("Message", key), b.ref("Message", parent), "REPLIES_TO", item.occurred_at, record=True)
    b.flush()
    return {b.ids[("Message", message_key(i.raw_meta, i.id))] for i in late
            if i.source in MAIL and ("Message", message_key(i.raw_meta, i.id)) in b.ids}


# --- one batch -----------------------------------------------------------------------------------------------------

PENDING = text("""
SELECT id, source, external_id, title, raw_meta, occurred_at, content_hash,
       CASE WHEN source IN ('email', 'gmail') THEN body_text END AS body_text
FROM source_items WHERE spined_hash IS DISTINCT FROM content_hash ORDER BY id LIMIT :n
""")


def _process(s: Session, rows, cards: dict) -> None:
    """Build one batch of records: replace what they built before, classify them, recompute their replies and their
    threads' flags, and mark them processed at the content hash that was read."""
    batch = [r.id for r in rows]
    s.execute(text("DELETE FROM spine_node_support WHERE source_item_id = ANY(:ids)"), {"ids": batch})
    s.execute(text("DELETE FROM spine_edge_support WHERE source_item_id = ANY(:ids)"), {"ids": batch})
    b = Builder(s, Context(s, rows, cards))
    for item in rows:
        b.build(item)
    b.flush()

    # The Message node each record is, and its time.
    is_message = {}
    for (type_, _key), n in b.nodes.items():
        if type_ == "Message":
            for rid, rep in n.records.items():
                if rep:
                    is_message[rid] = b.ids[(type_, _key)]
    messages = set(is_message.values())
    quoted.store(s, {is_message[r.id]: r.body_text for r in rows if r.source in MAIL and r.id in is_message})
    answered = _link_late_parents(s, rows, batch, cards)
    # The quotes this batch's messages may answer, and those that may name one of them (built just now, or rebuilt).
    times = [r.occurred_at for r in rows if r.id in is_message]
    changed = quoted.run(s, {is_message[r.id] for r in rows if r.source in MAIL and r.id in is_message}
                         | quoted.quoting(s, times))
    threads = _threads_of(s, messages)
    infer_replies(s, threads | _threads_of(s, changed | answered))

    s.execute(text("""
        UPDATE source_items si SET item_class = v.c, spined_hash = v.h
        FROM jsonb_to_recordset(CAST(:rows AS jsonb)) AS v(id bigint, c text, h text)
        WHERE si.id = v.id AND si.content_hash = v.h
    """), {"rows": _rows([{"id": r.id, "h": r.content_hash,
                           "c": item_class.classify(r.source, r.title, r.raw_meta, r.body_text)} for r in rows])})
    item_class.refresh_thread_flags(s, threads)
    dirty.mark(s, set(b.ids.values()), "spine")


# --- what lost its support -----------------------------------------------------------------------------------------

def settle(s: Session, cards: dict) -> dict:
    """Delete the queued nodes and edges no record supports any more, point what remains at records it still has,
    and re-derive what depended on what went. Returns {"deleted": nodes and edges removed, "pending": records marked
    for rebuilding}."""
    edge_q = s.execute(text("SELECT edge_id FROM spine_gc_edges ORDER BY edge_id")).scalars().all()
    node_q = s.execute(text("SELECT node_id FROM spine_gc_nodes ORDER BY node_id")).scalars().all()
    gone = s.execute(text("""
        DELETE FROM edges e WHERE e.id = ANY(:q)
          AND NOT EXISTS (SELECT 1 FROM spine_edge_support x WHERE x.edge_id = e.id)
        RETURNING e.id, e.src, e.dst, e.type, e.valid_from
    """), {"q": edge_q}).all() if edge_q else []
    doomed = s.execute(text(f"""
        SELECT n.id FROM nodes n WHERE n.id = ANY(:q)
          AND NOT EXISTS (SELECT 1 FROM spine_node_support x WHERE x.node_id = n.id)
          AND NOT (n.type = 'Person' AND n.key = '{OWNER_KEY}')
          AND NOT EXISTS (SELECT 1 FROM edges u WHERE u.src = n.id AND u.extractor = 'user')
          AND NOT EXISTS (SELECT 1 FROM edges u WHERE u.dst = n.id AND u.extractor = 'user')
          AND (n.type = ANY(:spine_types) OR (
              NOT EXISTS (SELECT 1 FROM edges o WHERE o.src = n.id AND o.extractor <> ALL(:spine) AND o.type <> 'SAME_AS')
              AND NOT EXISTS (SELECT 1 FROM edges o WHERE o.dst = n.id AND o.extractor <> ALL(:spine)
                              AND o.type <> 'SAME_AS')))
    """), {"q": node_q, "spine_types": list(SPINE_TYPES), "spine": list(SPINE_EXTRACTORS)}).scalars().all() \
        if node_q else []
    orphans = set()
    if doomed:
        # Replies that quoted a message about to go may now quote another one unambiguously.
        orphans = set(s.execute(text("""
            SELECT src FROM edges WHERE dst = ANY(:d) AND type = 'REPLIES_TO' AND extractor = :ex
        """), {"d": doomed, "ex": quoted.EXTRACTOR}).scalars())
        s.execute(text("DELETE FROM nodes WHERE id = ANY(:d)"), {"d": doomed})
    survivors = sorted(set(node_q) - set(doomed))
    if survivors:
        moved = s.execute(text("""
            UPDATE nodes n SET source_item_id = x.rep
            FROM (SELECT n.id,
                         CASE WHEN EXISTS (SELECT 1 FROM spine_node_support y WHERE y.node_id = n.id
                                           AND y.source_item_id = n.source_item_id AND y.represents)
                              THEN n.source_item_id
                              ELSE (SELECT min(y.source_item_id) FROM spine_node_support y
                                    WHERE y.node_id = n.id AND y.represents) END AS rep
                  FROM nodes n WHERE n.id = ANY(:ids)
                    AND EXISTS (SELECT 1 FROM spine_node_support y WHERE y.node_id = n.id)) x
            WHERE n.id = x.id AND n.source_item_id IS DISTINCT FROM x.rep
            RETURNING n.type, n.source_item_id
        """), {"ids": survivors}).all()
        # A message, file or event now read from another of its records: chunk that one.
        reread = sorted({sid for type_, sid in moved if sid is not None and type_ in ("Message", "File", "Event")})
        if reread:
            s.execute(text("UPDATE source_items SET chunked_hash = NULL WHERE id = ANY(:ids)"), {"ids": reread})
    kept = sorted(set(edge_q) - {g.id for g in gone})
    if kept:
        s.execute(text("""
            UPDATE edges e SET source_item_id = x.rep
            FROM (SELECT e.id,
                         CASE WHEN bool_or(y.source_item_id = e.source_item_id AND y.represents) THEN e.source_item_id
                              ELSE min(y.source_item_id) FILTER (WHERE y.represents) END AS rep
                  FROM edges e JOIN spine_edge_support y ON y.edge_id = e.id
                  WHERE e.id = ANY(:ids) GROUP BY e.id, e.source_item_id) x
            WHERE e.id = x.id AND e.source_item_id IS DISTINCT FROM x.rep
        """), {"ids": kept})
        span(s, kept)

    # Threads that lost a message, or a message's sender or recipient, and the times of the messages that went.
    lost_member = {g.dst for g in gone if g.type == "IN_THREAD"} - set(doomed)
    lost_part = {g.src if g.type == "RECEIVED" else g.dst for g in gone if g.type in ("SENT", "RECEIVED")}
    threads = lost_member | _threads_of(s, lost_part)
    times = [g.valid_from for g in gone if g.type == "IN_THREAD" and g.valid_from is not None]
    if doomed or gone:
        changed = quoted.run(s, orphans | quoted.quoting(s, times))
        infer_replies(s, threads | _threads_of(s, changed))
        item_class.refresh_thread_flags(s, threads)
        touched = {n for g in gone for n in (g.src, g.dst)} | set(survivors)
        alive = set(s.execute(text("SELECT id FROM nodes WHERE id = ANY(:ids)"), {"ids": sorted(touched)}).scalars())
        dirty.mark(s, alive, "spine")
        if doomed:
            dirty.mark_rebuild(s)
    if edge_q:
        s.execute(text("DELETE FROM spine_gc_edges WHERE edge_id = ANY(:q)"), {"q": edge_q})
    if node_q:
        # A node no record supports that the owner, a user fact or another stage still holds stays queued: it goes once
        # nothing holds it.
        s.execute(text("""
            DELETE FROM spine_gc_nodes q WHERE q.node_id = ANY(:q)
              AND (NOT EXISTS (SELECT 1 FROM nodes n WHERE n.id = q.node_id)
                   OR EXISTS (SELECT 1 FROM spine_node_support x WHERE x.node_id = q.node_id))
        """), {"q": node_q})
    return {"deleted": len(doomed) + len(gone), "pending": _recheck_cards(s, cards)}


def _recheck_cards(s: Session, cards: dict) -> int:
    """Make the identities contact cards name agree with the cards as they are now. A person no card names any more
    loses the card's name. The cards carrying an identifier whose graph disagrees with them (named or linked by a card
    while another person's card shares it, or unnamed while one card alone names it) are marked for rebuilding. Returns
    how many cards were marked."""
    s.execute(text("""
        UPDATE nodes SET name = CASE WHEN props->>'renamed' = 'true' THEN name ELSE key END, props = props - 'named_by'
        WHERE type = 'Person' AND props->>'named_by' = 'contacts' AND key <> ALL(:k)
    """), {"k": sorted(k for k, on in cards.items() if any(on.values()))})
    if not cards:
        return 0
    state = {r.key: (r.named, r.linked) for r in s.execute(text("""
        SELECT n.key, n.props->>'named_by' = 'contacts' AS named,
               EXISTS (SELECT 1 FROM edges e WHERE e.type = 'SAME_AS' AND e.extractor = 'contacts'
                       AND (e.src = n.id OR e.dst = n.id)) AS linked
        FROM nodes n WHERE n.type = 'Person' AND n.key = ANY(:k)
    """), {"k": sorted(cards)})}
    stale = set()
    for key, on in cards.items():
        if key not in state:
            continue
        names = {n for n in on.values() if n}
        named, linked = state[key]
        if bool(named) != (len(names) == 1) or (len(names) > 1 and linked):
            stale |= set(on)
    if not stale:
        return 0
    return s.execute(text("""
        UPDATE source_items SET spined_hash = NULL WHERE id = ANY(:ids) AND spined_hash = content_hash
    """), {"ids": sorted(stale)}).rowcount


# --- names ---------------------------------------------------------------------------------------------------------

NAME_FROM_HEADERS = text(r"""
WITH seen AS (
    SELECT lower(a->>'email') AS key, btrim(a->>'name', ' "') AS name
    FROM source_items si,
         LATERAL jsonb_array_elements(COALESCE(si.raw_meta->'from', '[]') || COALESCE(si.raw_meta->'to', '[]')
                                      || COALESCE(si.raw_meta->'cc', '[]')) a
    WHERE si.source IN ('gmail', 'email') AND COALESCE(a->>'name', '') <> ''
), counted AS (
    SELECT key, name, count(*) AS n FROM seen
    WHERE name !~ '@' AND name ~ '[[:alpha:]] [[:alpha:]]' GROUP BY key, name
), best AS (SELECT DISTINCT ON (key) key, name FROM counted ORDER BY key, n DESC, length(name) DESC, name)
UPDATE nodes p SET name = best.name
FROM best
WHERE p.type = 'Person' AND p.key = best.key AND p.name IS DISTINCT FROM best.name
  AND COALESCE(p.props->>'named_by', '') <> 'contacts' AND NOT COALESCE((p.props->>'renamed')::bool, false)
""")


def name_from_headers(s: Session) -> int:
    """Name each address by what most mail calls it ("Jordan Lee" in 50 headers beats "J Lee" in 26),
    whatever order items arrived in. A contact card or a user rename always wins."""
    return s.execute(NAME_FROM_HEADERS).rowcount


# --- the stage -----------------------------------------------------------------------------------------------------

def _analyze(s: Session) -> None:
    """Refresh the planner's statistics on what the spine writes in bulk: queries planned on a graph ten times
    smaller than the one they run on pick nested loops that take minutes."""
    s.execute(text("ANALYZE nodes, edges, spine_node_support, spine_edge_support, spine_quotes"))


RECHECKS = 3  # rounds of rebuilding contact cards a run settles; any left are pending for the next run


def run(s: Session) -> int:
    """Build every pending record, then settle what lost its support. Returns the number of records built."""
    cards = contact_cards(s)
    built = deleted = analyzed = rounds = 0
    while True:
        rows = s.execute(PENDING, {"n": BATCH}).all()
        if rows:
            _process(s, rows, cards)
            s.commit()
            built += len(rows)
            if built >= 2 * max(analyzed, BATCH):
                _analyze(s)
                s.commit()
                analyzed = built
            continue
        cards = contact_cards(s)  # as they are now, cards synced since the run began included
        settled = settle(s, cards)
        s.commit()
        deleted += settled["deleted"]
        rounds += 1
        if not settled["pending"] or rounds == RECHECKS:
            break
    if built or deleted:
        if built + deleted >= BATCH:
            _analyze(s)
        name_from_headers(s)  # each address's most common display name, across all mail
        s.commit()
    return built
