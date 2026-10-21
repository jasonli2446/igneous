"""Stage one: deterministic graph from metadata alone. Exactly correct, no model calls, idempotent."""
import json
import re
from itertools import pairwise

from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous.connectors.mac_contacts import norm_phone
from igneous.graph import dirty
from igneous.pipeline import advance, watermark

STAGE = "spine"
BATCH = 1000

UPSERT_NODE = text("""
INSERT INTO nodes (type, key, name, props, source_item_id)
VALUES (:type, :key, :name, CAST(:props AS jsonb), :sid)
ON CONFLICT (type, key) DO UPDATE SET
    name = CASE WHEN length(EXCLUDED.name) > length(nodes.name) THEN EXCLUDED.name ELSE nodes.name END,
    source_item_id = COALESCE(EXCLUDED.source_item_id, nodes.source_item_id),
    props = nodes.props || EXCLUDED.props
RETURNING id
""")

# These edges represent a discrete message or event, rather than an ongoing relationship.
POINT_IN_TIME_TYPES = frozenset({"SENT", "RECEIVED", "IN_THREAD", "REPLIES_TO", "ATTENDED"})

UPSERT_EDGE = text("""
INSERT INTO edges (src, dst, type, valid_from, valid_to, source_item_id)
VALUES (:src, :dst, :type, :valid_from, :valid_to, :sid)
ON CONFLICT (src, dst, type, extractor) DO UPDATE SET
    valid_from = CASE
        WHEN edges.type = 'MEMBER_OF' AND EXCLUDED.valid_from IS NOT NULL THEN
            CASE WHEN edges.valid_from IS NULL THEN EXCLUDED.valid_from
                 ELSE LEAST(edges.valid_from, EXCLUDED.valid_from) END
        ELSE COALESCE(edges.valid_from, EXCLUDED.valid_from)
    END,
    valid_to = CASE
        WHEN edges.type = 'MEMBER_OF' AND EXCLUDED.valid_to IS NOT NULL THEN
            CASE WHEN edges.valid_to IS NULL THEN EXCLUDED.valid_to
                 ELSE GREATEST(edges.valid_to, EXCLUDED.valid_to) END
        ELSE COALESCE(edges.valid_to, EXCLUDED.valid_to)
    END,
    source_item_id = COALESCE(edges.source_item_id, EXCLUDED.source_item_id)
""")

# Each invocation repairs at most BATCH legacy edges.  It is deliberately outside the Alembic migration:
# deployments can upgrade quickly, while normal spine runs resume this idempotent backfill safely.
BACKFILL_POINT_EDGES = text("""
WITH candidates AS (
    SELECT e.id, COALESCE(si.occurred_at, source_item.occurred_at, e.valid_from) AS observed_at
    FROM edges e
    LEFT JOIN source_items si ON si.id = e.source_item_id
    LEFT JOIN nodes source_node ON source_node.id = e.src
    LEFT JOIN source_items source_item ON source_item.id = source_node.source_item_id
    WHERE e.type = ANY(:types)
      AND (e.valid_from IS NULL OR e.valid_to IS NULL)
      AND COALESCE(si.occurred_at, source_item.occurred_at, e.valid_from) IS NOT NULL
    ORDER BY e.id
    LIMIT :limit
)
UPDATE edges e
SET valid_from = c.observed_at, valid_to = c.observed_at
FROM candidates c
WHERE e.id = c.id
""")

BACKFILL_MEMBERSHIPS = text("""
WITH candidates AS (
    SELECT m.id
    FROM edges m
    JOIN nodes person ON person.id = m.src AND person.type = 'Person'
    JOIN nodes organization ON organization.id = m.dst AND organization.type = 'Organization'
    WHERE m.type = 'MEMBER_OF'
      AND (m.valid_from IS NULL OR m.valid_to IS NULL)
      AND EXISTS (
          SELECT 1
          FROM edges observed
          LEFT JOIN source_items si ON si.id = observed.source_item_id
          WHERE observed.type = ANY(:types)
            AND ((observed.src = person.id AND observed.type IN ('SENT', 'ATTENDED'))
                 OR (observed.dst = person.id AND observed.type = 'RECEIVED'))
            AND COALESCE(si.occurred_at, observed.valid_from) IS NOT NULL
      )
    ORDER BY m.id
    LIMIT :limit
), bounds AS (
    SELECT m.id,
           min(COALESCE(si.occurred_at, observed.valid_from)) AS valid_from,
           max(COALESCE(si.occurred_at, observed.valid_from)) AS valid_to
    FROM edges m
    JOIN nodes person ON person.id = m.src
    JOIN candidates c ON c.id = m.id
    JOIN edges observed ON ((observed.src = person.id AND observed.type IN ('SENT', 'ATTENDED'))
                         OR (observed.dst = person.id AND observed.type = 'RECEIVED'))
    LEFT JOIN source_items si ON si.id = observed.source_item_id
    WHERE observed.type = ANY(:types)
    GROUP BY m.id
)
UPDATE edges m
SET valid_from = bounds.valid_from, valid_to = bounds.valid_to
FROM bounds
WHERE m.id = bounds.id
""")


class Builder:
    def __init__(self, s: Session):
        self.s = s
        self.cache: dict[tuple[str, str], int] = {}
        self.touched: set[int] = set()

    def node(self, type_: str, key: str, name: str, props: dict | None = None, sid: int | None = None) -> int:
        k = (type_, key)
        if k not in self.cache:
            self.cache[k] = self.s.execute(UPSERT_NODE, {
                "type": type_, "key": key, "name": name or key,
                "props": json.dumps(props or {}), "sid": sid}).scalar_one()
        self.touched.add(self.cache[k])
        return self.cache[k]

    def edge(self, src: int, dst: int, type_: str, at=None, sid: int | None = None):
        # Point-in-time edges have a closed interval. MEMBER_OF uses every observation to widen its interval.
        valid_to = at if type_ in POINT_IN_TIME_TYPES or type_ == "MEMBER_OF" else None
        self.s.execute(UPSERT_EDGE, {"src": src, "dst": dst, "type": type_,
                                     "valid_from": at, "valid_to": valid_to, "sid": sid})


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


def _person(b: Builder, addr: dict, name: str, observed_at=None, sid: int | None = None) -> int:
    pid = b.node("Person", addr["email"], name, {"email": addr["email"]})
    # Address domains alone are not employment evidence. Organization membership is
    # derived later from owner-sent mail, contact-card companies, or calendar data.
    return pid


def build_email(b: Builder, item) -> None:
    meta = item.raw_meta
    msg_key = meta.get("fingerprint") or meta.get("message_id") or f"item:{item.id}"
    mid = b.node("Message", msg_key, item.title or "(no subject)", {"source": item.source}, sid=item.id)

    for addr in meta.get("from", [])[:1]:
        b.edge(_person(b, addr, _name_from(addr, meta.get("x_from")), item.occurred_at, item.id), mid,
               "SENT", item.occurred_at, item.id)
    for field in ("to", "cc", "bcc"):
        for addr in meta.get(field, []):
            b.edge(mid, _person(b, addr, _name_from(addr, None), item.occurred_at, item.id), "RECEIVED",
                   item.occurred_at, item.id)

    tkey = _thread_key(meta, item.title)
    tid = b.node("Thread", tkey, REPLY_PREFIX.sub("", item.title or "") or "(no subject)")
    b.edge(mid, tid, "IN_THREAD", item.occurred_at, item.id)

    if meta.get("in_reply_to"):
        parent = b.node("Message", meta["in_reply_to"], "(not ingested)")
        b.edge(mid, parent, "REPLIES_TO", item.occurred_at, item.id)


# Header-less corpora (Enron) carry no In-Reply-To, so infer it: within a thread, a "Re:" message replies
# to the latest earlier message from a different sender. Re-derived wholesale; cheap at this scale.
INFER_REPLIES = text("""
WITH m AS (
    SELECT msg.id, t.dst AS thread, si.occurred_at, si.title,
           (SELECT e.src FROM edges e WHERE e.dst = msg.id AND e.type = 'SENT' LIMIT 1) AS sender
    FROM nodes msg
    JOIN source_items si ON si.id = msg.source_item_id
    JOIN edges t ON t.src = msg.id AND t.type = 'IN_THREAD'
    WHERE msg.type = 'Message' AND si.occurred_at IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM edges r WHERE r.src = msg.id AND r.type = 'REPLIES_TO')
)
INSERT INTO edges (src, dst, type, valid_from, valid_to, extractor)
SELECT child.id, parent.id, 'REPLIES_TO', child.occurred_at, child.occurred_at, 'spine-inferred'
FROM m child
CROSS JOIN LATERAL (
    SELECT p.id FROM m p
    WHERE p.thread = child.thread AND p.occurred_at < child.occurred_at
      AND p.sender IS DISTINCT FROM child.sender
    ORDER BY p.occurred_at DESC LIMIT 1
) parent
WHERE child.title ~* '^\\s*re\\s*:'
ON CONFLICT (src, dst, type, extractor) DO NOTHING
""")


def build_chat(b: Builder, item) -> None:
    """A conversation-day from Messages: one Message node for the day, a Thread per chat, a Person per handle.
    Phone numbers and Apple IDs are the keys; entity resolution can later join them to email identities."""
    meta = item.raw_meta
    mid = b.node("Message", f"imessage:{item.external_id}", item.title or "(chat)", {"source": "imessage"},
                 sid=item.id)
    # Group chats are marked so the graph can treat them as groups ("who else is in the Draw Steel chat").
    tid = b.node("Thread", f"imessage-chat:{meta.get('chat')}", (item.title or "").rsplit(" — ", 1)[0] or "chat",
                 {"source": "imessage", "group": bool(meta.get("group")) or len(meta.get("participants", [])) > 1})
    b.edge(mid, tid, "IN_THREAD", item.occurred_at, item.id)
    me = b.node("Person", "me", "Me", {"self": True})
    senders = set(meta.get("senders", []))
    members = {h.lower(): h for h in meta.get("participants", [])}
    # Everyone in the chat received the day's conversation; whoever spoke also SENT it. (Earlier this gave each
    # person only one of the two, so two members who both spoke never looked like correspondents.)
    for key, handle in members.items():
        if "@" not in key:
            key = norm_phone(key) or key  # '+16145551234' / '6145551234' -> one key, matching contact cards
        pid = b.node("Person", key, handle, {"handle": handle})
        b.edge(mid, pid, "RECEIVED", item.occurred_at, item.id)
        if handle in senders:
            b.edge(pid, mid, "SENT", item.occurred_at, item.id)
    b.edge(mid, me, "RECEIVED", item.occurred_at, item.id)
    if "me" in senders:
        b.edge(me, mid, "SENT", item.occurred_at, item.id)


def build_ai_chat(b: Builder, item) -> None:
    """One AI session-day: a Message from you in a Thread for the session. The repo it ran in becomes a
    Folder-like anchor (key `repo:<name>`) that the entity stage can join to the matching GitHub repo, local
    folder, or project, which is how an assistant conversation lands in the right project."""
    meta = item.raw_meta
    tool = meta.get("tool", "ai")
    mid = b.node("Message", f"ai:{item.external_id}", item.title or "(AI session)", {"source": "ai_chats", "tool": tool},
                 sid=item.id)
    tid = b.node("Thread", f"ai-session:{tool}/{meta.get('session')}", (item.title or "AI session").rsplit(" (", 1)[0],
                 {"source": "ai_chats", "tool": tool})
    b.edge(mid, tid, "IN_THREAD", item.occurred_at, item.id)
    me = b.node("Person", "me", "Me", {"self": True})
    b.edge(me, mid, "SENT", item.occurred_at, item.id)
    if meta.get("repo"):
        repo = b.node("Folder", f"repo:{meta['repo'].lower()}", meta["repo"], {"path": meta.get("cwd"), "kind": "repo"})
        b.edge(repo, tid, "CONTAINS", item.occurred_at, item.id)


def build_event(b: Builder, item) -> None:
    """Calendar event: one Event node per recurring series (a weekly class is one node, not 15), so attendees'
    ATTENDED edges dedupe to one per series. Each occurrence stays its own source item, which is what
    relationship strength counts. Organizer and attendees ATTENDED it (decliners excluded)."""
    meta = item.raw_meta
    key = meta.get("event_key") or f"{item.source}:{meta.get('series_key') or item.external_id}"
    eid = b.node("Event", key, item.title or "(event)",
                 {"series": bool(meta.get("recurring"))}, sid=item.id)
    people = list(meta.get("attendees", []))
    if meta.get("organizer", {}).get("email"):
        people.append({**meta["organizer"], "response": "accepted"})
    for a in people:
        if a.get("response") == "declined" or "@" not in a.get("email", ""):
            continue
        b.edge(_person(b, a, _name_from(a, None), item.occurred_at, item.id), eid, "ATTENDED",
               item.occurred_at, item.id)


def build_contact(b: Builder, item) -> None:
    """A contact card names one human with several addresses and numbers. It names the matching Person nodes
    (so a Messages handle shows as "Jane Doe", not "+16145551234") and emits SAME_AS between every
    identity on the card: ground truth for entity resolution (extractor 'contacts', which `resolve --undo`
    leaves alone)."""
    meta = item.raw_meta
    name = item.title
    keys = [e for e in meta.get("emails", [])] + [norm_phone(p) or p for p in meta.get("phones", []) if p]
    ids = []
    for k in keys:
        pid = b.node("Person", k, name, {"handle": k} if "@" not in k else {"email": k})
        # A contact card is the authority on a person's name: overwrite handle- or address-derived names.
        b.s.execute(text("UPDATE nodes SET name = :n, props = props || '{\"named_by\": \"contacts\"}' WHERE id = :id"),
                    {"n": name, "id": pid})
        ids.append(pid)
    for a, c in pairwise(ids):
        s0, s1 = sorted((a, c))
        b.s.execute(text("""
            INSERT INTO edges (src, dst, type, extractor, confidence, source_item_id)
            VALUES (:a, :b, 'SAME_AS', 'contacts', 1.0, :sid) ON CONFLICT (src, dst, type, extractor) DO NOTHING
        """), {"a": s0, "b": s1, "sid": item.id})


def build_github(b: Builder, item) -> None:
    """Represent GitHub activity as authored messages anchored to a repository.

    GitHub logins are stable person keys. A verified commit email is a separate identity
    and receives a trusted SAME_AS edge, rather than guessing from display names.
    """
    meta = item.raw_meta
    repo_name = meta.get("repo", "github")
    repo = b.node("Folder", f"repo:{repo_name.lower()}", repo_name, {"kind": "repo", "source": "github"})
    mid = b.node("Message", f"github:{item.external_id}", item.title or repo_name, {"source": "github"}, sid=item.id)
    b.edge(repo, mid, "CONTAINS", item.occurred_at, item.id)
    thread = b.node("Thread", f"github:{repo_name}:{meta.get('kind')}:{meta.get('number', meta.get('day', item.external_id))}",
                    item.title or repo_name, {"source": "github"})
    b.edge(mid, thread, "IN_THREAD", item.occurred_at, item.id)
    identities = [meta.get("author", {}), *meta.get("commits", [])]
    for identity in identities:
        login, email = identity.get("login"), identity.get("email")
        if not login:
            continue
        pid = b.node("Person", f"github:{login.lower()}", login, {"github_login": login})
        b.edge(pid, mid, "SENT", item.occurred_at, item.id)
        if email and email.lower() != "notifications@github.com":
            email_id = b.node("Person", email.lower(), email, {"email": email.lower()})
            a, c = sorted((pid, email_id))
            b.s.execute(text("""
                INSERT INTO edges (src, dst, type, extractor, confidence, source_item_id)
                VALUES (:a, :b, 'SAME_AS', 'github', 1.0, :sid)
                ON CONFLICT (src, dst, type, extractor) DO NOTHING
            """), {"a": a, "b": c, "sid": item.id})
    for comment in meta.get("comments", []):
        login = comment.get("login")
        if login:
            pid = b.node("Person", f"github:{login.lower()}", login, {"github_login": login})
            b.edge(pid, mid, "SENT", item.occurred_at, item.id)


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
    fid = b.node("File", fkey, item.title or item.external_id,
                 {"source": item.source, "path": path}, sid=item.id)
    # Drive preserves every parent ID in metadata: a file can belong to more than one folder,
    # which cannot be represented faithfully by a single filesystem-style path.
    parents = item.raw_meta.get("parents")
    if isinstance(parents, list) and all(isinstance(parent, str) for parent in parents):
        for parent in parents:
            folder = b.node("Folder", f"{item.source}:drive/{parent}", parent, {"path": f"drive/{parent}"})
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
        b.edge(repo, child, "CONTAINS", item.occurred_at, item.id)


def backfill_temporal(s: Session, limit: int = BATCH) -> int:
    """Repair one bounded batch of legacy temporal data without blocking an upgrade."""
    params = {"types": list(POINT_IN_TIME_TYPES), "limit": limit}
    points = s.execute(BACKFILL_POINT_EDGES, params).rowcount or 0
    memberships = s.execute(BACKFILL_MEMBERSHIPS, params).rowcount or 0
    return points + memberships


def run(s: Session) -> int:
    since = watermark(s, STAGE)
    rows = s.execute(text("""
        SELECT id, source, external_id, title, raw_meta, occurred_at, updated_at
        FROM source_items WHERE updated_at > :since ORDER BY updated_at, id
    """), {"since": since}).all()
    b = Builder(s)
    for i, item in enumerate(rows, 1):
        b.touched.clear()
        if item.source in ("email", "gmail"):
            build_email(b, item)
        elif item.source == "imessage":
            build_chat(b, item)
        elif item.source == "ai_chats":
            build_ai_chat(b, item)
        elif item.source == "github":
            build_github(b, item)
        elif item.source == "gcal":
            build_event(b, item)
        elif item.source in ("gcontacts", "maccontacts"):
            build_contact(b, item)
        elif item.source in ("fs", "drive", "notes"):
            build_file(b, item)
        dirty.mark(s, b.touched, f"source item {item.id} changed")
        if i % BATCH == 0:
            advance(s, STAGE, item.updated_at)
            s.commit()
    if rows:
        advance(s, STAGE, rows[-1].updated_at)
        s.commit()
        from igneous.graph import quoted

        quoted.run(s)               # exact parents from quoted "Original Message" headers first
        s.execute(INFER_REPLIES)    # then the subject+participant heuristic for whatever is left
        from igneous.graph.history import derive_introductions
        derive_introductions(s)     # deterministic, source-cited three-person introductions
    backfill_temporal(s)
    s.commit()
    return len(rows)
