"""Structure-aware chunking. Email: one chunk per message with quoted tails stripped."""
import json
import re

from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous.pipeline import advance, watermark

STAGE = "chunk"
MAX_CHARS = 2000  # ~500 tokens
OVERLAP = 200

# Lines that start a quoted reply or forwarded block in Enron/Outlook mail.
QUOTE_START = re.compile(
    r"^\s*(-{2,}\s*Original Message\s*-{2,}|-{5,}\s*Forwarded by|From:\s.+Sent:|"
    r"On .+ wrote:|>\s|_{10,})", re.I | re.M)


def strip_quoted(body: str) -> str:
    m = QUOTE_START.search(body)
    return (body[: m.start()] if m else body).strip()


def split(t: str) -> list[str]:
    if len(t) <= MAX_CHARS:
        return [t] if t else []
    out, i = [], 0
    while i < len(t):
        end = min(i + MAX_CHARS, len(t))
        if end < len(t):  # break on a paragraph or sentence boundary when one is close
            cut = max(t.rfind("\n\n", i, end), t.rfind(". ", i, end))
            if cut > i + MAX_CHARS // 2:
                end = cut + 1
        out.append(t[i:end].strip())
        if end >= len(t):
            break
        i = end - OVERLAP
    return [c for c in out if c]


def chunks_for(item) -> list[str]:
    body = item.body_text or ""
    if item.source == "gcal":  # title + time + people so "meeting with Sam last week" finds it
        who = ", ".join(a.get("name") or a["email"] for a in (item.raw_meta or {}).get("attendees", [])[:20])
        head = f"Event: {item.title}\nWhen: {item.occurred_at}\nAttendees: {who}\n"
        return [head + c for c in split(body)] or [head.strip()]
    if item.source in ("email", "gmail"):
        body = strip_quoted(body)
        header = f"Subject: {item.title}\n" if item.title else ""
        return [header + c for c in split(body)] or ([header.strip()] if header else [])
    return split(body)


def run(s: Session) -> int:
    since = watermark(s, STAGE)
    rows = s.execute(text("""
        SELECT si.id, si.source, si.title, si.body_text, si.occurred_at, si.updated_at, si.raw_meta,
               n.id AS node_id
        FROM source_items si
        LEFT JOIN nodes n ON n.source_item_id = si.id AND n.type IN ('Message', 'File', 'Event')
        -- contact cards carry no text worth searching
        WHERE si.source NOT IN ('gcontacts', 'maccontacts') AND si.updated_at > :since
        ORDER BY si.updated_at, si.id
    """), {"since": since}).all()
    for item in rows:
        s.execute(text("DELETE FROM chunks WHERE source_item_id = :id"), {"id": item.id})
        if item.node_id is None:
            continue  # a duplicate copy (e.g. same email in two Enron folders); its node's canonical item is chunked

        meta = json.dumps({"source": item.source, "title": item.title,
                           "occurred_at": item.occurred_at.isoformat() if item.occurred_at else None})
        for ord_, c in enumerate(chunks_for(item)):
            s.execute(text("""
                INSERT INTO chunks (source_item_id, node_id, ord, text, meta)
                VALUES (:sid, :nid, :ord, :text, CAST(:meta AS jsonb))
            """), {"sid": item.id, "nid": item.node_id, "ord": ord_, "text": c, "meta": meta})
    if rows:
        advance(s, STAGE, rows[-1].updated_at)
    s.commit()
    return len(rows)
