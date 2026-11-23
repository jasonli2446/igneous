"""Structure-aware chunking, every chunk under its record's title so that a search for a subject, a file name or a
conversation finds each part of it. Email: one chunk per message with quoted tails stripped, under "Subject:".
Calendar events: title, time and attendees. Parsed documents (PDF, DOCX, PPTX, XLSX): chunks follow the pages,
sections, slides and sheets the parser recorded, and each says where in the document it is. Everything else (local
files, notes, AI chats, GitHub, Takeout days, messages) is split by length, the title first in every chunk.

`run` chunks the records whose content changed since it last read them (`chunked_hash`), once the spine has built
them, and every record chunked under an older LAYOUT, once."""
import json
import re

from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous import source_catalog

MAX_CHARS = 2000  # ~500 tokens
OVERLAP = 200
# Recorded in each chunk's meta. An item chunked under an older layout is chunked again once; an item whose chunks
# come out the same keeps them and their vectors, so only changed chunks are embedded again.
LAYOUT = 3

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


def titled(title: str | None, parts: list[str]) -> list[str]:
    """Each part with the record's title on its first line, unless the part already opens with it (a note whose
    first line is its title)."""
    if not title:
        return parts
    return [part if part.startswith(title) else f"{title}\n{part}" for part in parts]


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
    units = sections(item)
    if units:
        return packed(item.title or "", units)
    return titled(item.title, split(body))


BATCH = 500

# Records whose content changed since chunk last read them, once the spine has built that version (a record is chunked
# under the node it is), and records chunked under an older layout. `chunked_hash` is written back as the hash read: a
# record that changes meanwhile stays pending.
PENDING = text("""
    SELECT si.id, si.source, si.title, si.body_text, si.occurred_at, si.raw_meta, si.content_hash, n.id AS node_id
    FROM source_items si
    LEFT JOIN nodes n ON (n.source_item_id = si.id AND n.type IN ('Message', 'File'))
                      OR (si.source = 'gcal' AND n.type = 'Event'
                          AND n.key = COALESCE(si.raw_meta->>'event_key', 'gcal:' || COALESCE(si.raw_meta->>'series_key', si.external_id))
                          AND n.source_item_id = si.id)
    WHERE ((si.chunked_hash IS DISTINCT FROM si.content_hash AND si.spined_hash = si.content_hash)
           OR si.id = ANY(CAST(:again AS bigint[])))
      AND si.id > :after
    ORDER BY si.id LIMIT :n
""")


def run(s: Session) -> int:
    """Chunk every pending record (PENDING) and every record chunked under an older LAYOUT, one batch per
    transaction; returns how many records it read. A record whose chunks come out the same keeps them, vectors
    included; the others' chunks are replaced and wait for the embed stage."""
    again, after, done = _older_layout(s), 0, 0
    named = None
    while rows := s.execute(PENDING, {"again": again, "after": after, "n": BATCH}).all():
        # Index chat text with contact names, not numbers, so "what did Sam say" finds it (lexically and densely).
        from igneous.graph.traversal import handle_names, name_handles
        if named is None and any(r.source == "imessage" for r in rows):
            named = handle_names(s)
        existing = dict(s.execute(text("""
            SELECT source_item_id, array_agg(text ORDER BY ord) FROM chunks
            WHERE source_item_id = ANY(CAST(:ids AS bigint[])) GROUP BY 1
        """), {"ids": [r.id for r in rows]}).all())
        for item in rows:
            # Contact cards carry no text worth searching; a duplicate copy (the same email in two Enron folders) is
            # searched through the record its node points at.
            if item.node_id is None or item.source in source_catalog.CONTACTS:
                if item.id in existing:
                    s.execute(text("DELETE FROM chunks WHERE source_item_id = :id"), {"id": item.id})
                continue
            texts = chunks_for(item)
            if item.source == "imessage":
                texts = [name_handles(c, named or {}) for c in texts]
            meta = json.dumps({"source": item.source, "title": item.title, "layout": LAYOUT,
                               "occurred_at": item.occurred_at.isoformat() if item.occurred_at else None})
            if existing.get(item.id) == texts:
                s.execute(text("UPDATE chunks SET node_id = :nid, meta = CAST(:meta AS jsonb) "
                               "WHERE source_item_id = :sid"), {"sid": item.id, "nid": item.node_id, "meta": meta})
                continue
            if item.id in existing:
                s.execute(text("DELETE FROM chunks WHERE source_item_id = :id"), {"id": item.id})
            for ord_, c in enumerate(texts):
                s.execute(text("""
                    INSERT INTO chunks (source_item_id, node_id, ord, text, meta)
                    VALUES (:sid, :nid, :ord, :text, CAST(:meta AS jsonb))
                """), {"sid": item.id, "nid": item.node_id, "ord": ord_, "text": c, "meta": meta})
        s.execute(text("""
            UPDATE source_items si SET chunked_hash = v.h
            FROM unnest(CAST(:ids AS bigint[]), CAST(:hashes AS text[])) AS v(id, h)
            WHERE si.id = v.id AND si.content_hash = v.h
        """), {"ids": [r.id for r in rows], "hashes": [r.content_hash for r in rows]})
        s.commit()
        after, done = rows[-1].id, done + len(rows)
    return done


def _older_layout(s: Session) -> list[int]:
    """Items whose chunks were made under an older LAYOUT: chunked again once (run keeps those that don't change)."""
    return list(s.execute(text("""
        SELECT DISTINCT source_item_id FROM chunks WHERE COALESCE((meta->>'layout')::int, 1) < :layout
    """), {"layout": LAYOUT}).scalars())
