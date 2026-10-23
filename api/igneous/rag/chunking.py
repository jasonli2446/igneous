"""Structure-aware chunking. Email: one chunk per message with quoted tails stripped. Parsed documents (PDF, DOCX,
PPTX, XLSX): chunks follow the pages, sections, slides and sheets the parser recorded (#186), and each chunk says
where in the document it is."""
import json
import re

from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous.pipeline import advance, watermark

STAGE = "chunk"
MAX_CHARS = 2000  # ~500 tokens
OVERLAP = 200
LAYOUT = 2  # chunk meta 'layout': documents chunked by an older layout are re-chunked once

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


def sections(item) -> list[tuple[str, str]] | None:
    """(where, text) for each page, slide, sheet or heading section of a parsed document, in order. None when it
    has no boundaries, or they can't be trusted: a local file imported before boundaries followed redaction."""
    meta, body = item.raw_meta or {}, item.body_text or ""
    bounds = meta.get("boundaries") or []
    if not bounds or (meta.get("redactions") and not meta.get("boundaries_redacted")):
        return None
    if any(not (0 <= b["start"] <= b["end"] <= len(body)) for b in bounds):
        return None
    heads = [b for b in bounds if b["kind"] == "heading"]
    tables = [b for b in bounds if b["kind"] == "table"]
    if heads or tables:  # DOCX: a section runs from its heading to the next; the tables follow all the text
        stop = tables[0]["start"] if tables else len(body)
        starts = [(0, "")] + [(h["start"], h["title"]) for h in heads]
        units = [(where, body[a:(starts[i + 1][0] if i + 1 < len(starts) else stop)])
                 for i, (a, where) in enumerate(starts)]
        units += [(f"table {t['index']}", body[t["start"]:t["end"]]) for t in tables]
    else:
        units = [(f"sheet {b['name']}" if b["kind"] == "sheet" else f"{b['kind']} {b['index']}",
                  body[b["start"]:b["end"]]) for b in bounds]
    return [(where, unit.strip()) for where, unit in units if unit.strip()]


def packed(title: str, units: list[tuple[str, str]]) -> list[str]:
    """Whole units, small neighbours packed together up to MAX_CHARS; a longer unit is split on its own. Each chunk
    opens with the document's name and where in it the chunk is ("plan.pdf, page 3 to page 5")."""
    out: list[str] = []
    group: list[tuple[str, str]] = []

    def head(wheres: list[str]) -> str:
        wheres = [w for w in wheres if w]
        parts = [p for p in (title, f"{wheres[0]} to {wheres[-1]}" if len(wheres) > 1 else "".join(wheres)) if p]
        return ", ".join(parts) + "\n" if parts else ""

    def flush():
        if group:
            out.append(head([w for w, _ in group]) + "\n\n".join(t for _, t in group))
            group.clear()
    for where, unit in units:
        if len(unit) > MAX_CHARS:
            flush()
            out += [head([where]) + part for part in split(unit)]
            continue
        if group and sum(len(t) + 2 for _, t in group) + len(unit) > MAX_CHARS:
            flush()
        group.append((where, unit))
    flush()
    return out


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
    if item.source.startswith("takeout_"):  # a day of searches or visits: the title says which product and day
        head = f"{item.title}\n" if item.title else ""
        return [head + c for c in split(body)]
    units = sections(item)
    if units:
        return packed(item.title or "", units)
    return split(body)


def run(s: Session) -> int:
    since = watermark(s, STAGE)
    rows = s.execute(text("""
        SELECT si.id, si.source, si.title, si.body_text, si.occurred_at, si.updated_at, si.raw_meta,
               n.id AS node_id
        FROM source_items si
        LEFT JOIN nodes n ON (n.source_item_id = si.id AND n.type IN ('Message', 'File'))
                          OR (si.source = 'gcal' AND n.type = 'Event'
                              AND n.key = COALESCE(si.raw_meta->>'event_key', 'gcal:' || COALESCE(si.raw_meta->>'series_key', si.external_id))
                              AND n.source_item_id = si.id)
        -- contact cards carry no text worth searching
        WHERE si.source NOT IN ('gcontacts', 'maccontacts') AND (si.updated_at > :since OR si.id = ANY(CAST(:again AS bigint[])))
        ORDER BY si.updated_at, si.id
    """), {"since": since, "again": _older_layout(s)}).all()
    # Index chat text with contact names, not numbers, so "what did Sam say" finds it (lexically and densely).
    from igneous.graph.traversal import handle_names, name_handles
    named = handle_names(s) if any(r.source == "imessage" for r in rows) else {}
    for item in rows:
        s.execute(text("DELETE FROM chunks WHERE source_item_id = :id"), {"id": item.id})
        if item.node_id is None:
            continue  # a duplicate copy (e.g. same email in two Enron folders); its node's canonical item is chunked

        meta = json.dumps({"source": item.source, "title": item.title, "layout": LAYOUT,
                           "occurred_at": item.occurred_at.isoformat() if item.occurred_at else None})
        for ord_, c in enumerate(chunks_for(item)):
            if item.source == "imessage":
                c = name_handles(c, named)
            s.execute(text("""
                INSERT INTO chunks (source_item_id, node_id, ord, text, meta)
                VALUES (:sid, :nid, :ord, :text, CAST(:meta AS jsonb))
            """), {"sid": item.id, "nid": item.node_id, "ord": ord_, "text": c, "meta": meta})
    if rows and rows[-1].updated_at > since:  # re-chunking old documents must not move the watermark back
        advance(s, STAGE, rows[-1].updated_at)
    s.commit()
    return len(rows)


def _older_layout(s: Session) -> list[int]:
    """Parsed documents whose chunks predate LAYOUT: re-chunked once along their boundaries. Nothing else is,
    so nothing else is re-embedded."""
    return list(s.execute(text("""
        SELECT DISTINCT c.source_item_id FROM chunks c JOIN source_items si ON si.id = c.source_item_id
        WHERE jsonb_array_length(COALESCE(si.raw_meta->'boundaries', '[]')) > 0
          AND COALESCE((c.meta->>'layout')::int, 1) < :layout
    """), {"layout": LAYOUT}).scalars())
