"""Stage one: deterministic graph from metadata alone. Exactly correct, no model calls, idempotent."""
import json
import re

from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous.pipeline import advance, watermark

STAGE = "spine"
BATCH = 1000

UPSERT_NODE = text("""
INSERT INTO nodes (type, key, name, props, source_item_id)
VALUES (:type, :key, :name, CAST(:props AS jsonb), :sid)
ON CONFLICT (type, key) DO UPDATE SET
    name = CASE WHEN length(EXCLUDED.name) > length(nodes.name) THEN EXCLUDED.name ELSE nodes.name END,
    source_item_id = COALESCE(EXCLUDED.source_item_id, nodes.source_item_id)
RETURNING id
""")

UPSERT_EDGE = text("""
INSERT INTO edges (src, dst, type, valid_from, source_item_id)
VALUES (:src, :dst, :type, :at, :sid)
ON CONFLICT (src, dst, type, extractor) DO NOTHING
""")


class Builder:
    def __init__(self, s: Session):
        self.s = s
        self.cache: dict[tuple[str, str], int] = {}

    def node(self, type_: str, key: str, name: str, props: dict | None = None, sid: int | None = None) -> int:
        k = (type_, key)
        if k not in self.cache:
            self.cache[k] = self.s.execute(UPSERT_NODE, {
                "type": type_, "key": key, "name": name or key,
                "props": json.dumps(props or {}), "sid": sid}).scalar_one()
        return self.cache[k]

    def edge(self, src: int, dst: int, type_: str, at=None, sid: int | None = None):
        self.s.execute(UPSERT_EDGE, {"src": src, "dst": dst, "type": type_, "at": at, "sid": sid})


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


def _person(b: Builder, addr: dict, name: str) -> int:
    pid = b.node("Person", addr["email"], name, {"email": addr["email"]})
    domain = addr["email"].split("@")[-1]
    org = b.node("Organization", domain, domain)
    b.edge(pid, org, "MEMBER_OF")
    return pid


def build_email(b: Builder, item) -> None:
    meta = item.raw_meta
    msg_key = meta.get("fingerprint") or meta.get("message_id") or f"item:{item.id}"
    mid = b.node("Message", msg_key, item.title or "(no subject)", {"source": item.source}, sid=item.id)

    for addr in meta.get("from", [])[:1]:
        b.edge(_person(b, addr, _name_from(addr, meta.get("x_from"))), mid, "SENT", item.occurred_at, item.id)
    for field in ("to", "cc", "bcc"):
        for addr in meta.get(field, []):
            b.edge(mid, _person(b, addr, _name_from(addr, None)), "RECEIVED", item.occurred_at, item.id)

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
INSERT INTO edges (src, dst, type, valid_from, extractor)
SELECT child.id, parent.id, 'REPLIES_TO', child.occurred_at, 'spine-inferred'
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


def build_file(b: Builder, item) -> None:
    fid = b.node("File", f"{item.source}:{item.external_id}", item.title or item.external_id,
                 {"source": item.source, "path": item.external_id}, sid=item.id)
    parent = item.external_id.rsplit("/", 1)[0] if "/" in item.external_id else ""
    child = fid
    while parent:
        folder = b.node("Folder", f"{item.source}:{parent}", parent.rsplit("/", 1)[-1], {"path": parent})
        b.edge(folder, child, "CONTAINS")
        child = folder
        parent = parent.rsplit("/", 1)[0] if "/" in parent else ""


def run(s: Session) -> int:
    since = watermark(s, STAGE)
    rows = s.execute(text("""
        SELECT id, source, external_id, title, raw_meta, occurred_at, updated_at
        FROM source_items WHERE updated_at > :since ORDER BY updated_at, id
    """), {"since": since}).all()
    b = Builder(s)
    for i, item in enumerate(rows, 1):
        if item.source in ("email", "gmail"):
            build_email(b, item)
        elif item.source in ("fs", "drive"):
            build_file(b, item)
        if i % BATCH == 0:
            advance(s, STAGE, item.updated_at)
            s.commit()
    if rows:
        advance(s, STAGE, rows[-1].updated_at)
        s.execute(INFER_REPLIES)
    s.commit()
    return len(rows)
