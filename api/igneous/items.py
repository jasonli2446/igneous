"""Source items as the reader sees them: full text, safe metadata and where to open the original.

`GET /items/{id}` backs the in-app reader; `POST /items/{id}/open` opens a local file in its default macOS app;
`GET /graph/node/{id}/sources` lists the items that make a node up. Read-only: nothing here writes to a source.
"""
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous.db import get_session

router = APIRouter()

# Allow-list of raw_meta keys the reader may show. Anything else (tokens, cursors, paths) never leaves the API.
SAFE_META = ("from", "to", "cc", "calendar", "attendees", "organizer", "start", "end", "repo", "kind", "number",
             "author", "product", "chat", "group", "tool", "day", "labels", "original_mime", "modified_time")
LOCAL_SOURCES = ("fs",)


def _account(meta: dict) -> str:
    return quote(str(meta.get("account") or "0"), safe="@.")


def open_target(source: str, external_id: str, meta: dict | None, occurred_at: datetime | None = None) -> dict:
    """Where the original lives: {open_url, open_label, local_path}. Sources with no external home (iMessage,
    AI chats, Takeout, Apple Notes) return all-None and are read in the app."""
    meta = meta or {}
    url = label = local = None
    if source == "gmail" and external_id:
        url = f"https://mail.google.com/mail/?authuser={_account(meta)}#all/{quote(external_id, safe='')}"
        label = "Open in Gmail"
    elif source == "gcal" and occurred_at:  # htmlLink isn't stored: the day view is the closest stable link
        url = f"https://calendar.google.com/calendar/r/day/{occurred_at:%Y/%m/%d}?authuser={_account(meta)}"
        label = "Open in Calendar"
    elif source == "drive" and meta.get("web_view_link"):
        url, label = meta["web_view_link"], "Open in Drive"
    elif source == "github" and meta.get("url"):
        url, label = meta["url"], "Open on GitHub"
    elif source in LOCAL_SOURCES and meta.get("path"):
        local, label = meta["path"], "Open file"
    if url and not url.startswith("https://"):  # only ever hand the browser an https link
        url = label = None
    return {"open_url": url, "open_label": label, "local_path": local}


def resolve_local(path: str | None, roots: list[Path]) -> Path | None:
    """`root_key/relative` (as the filesystem connector stores it) -> a real file under a configured root, or None."""
    from igneous.connectors.filesystem import _root_key
    if not path or "/" not in path:
        return None
    key, rel = path.split("/", 1)
    for root in roots:
        root = root.expanduser().resolve()
        if _root_key(root) != key:
            continue
        target = (root / rel).resolve()
        if target.is_relative_to(root) and target.exists():
            return target
    return None


def safe_meta(meta: dict | None) -> dict:
    return {k: v for k, v in (meta or {}).items() if k in SAFE_META}


def snippet(body: str | None, n: int = 160) -> str:
    return " ".join((body or "").split())[:n]


def item_card(row: dict) -> dict:
    """The compact shape lists use (evidence, sources): enough to show, Open and View without another request."""
    t = open_target(row["source"], row.get("external_id") or "", row.get("raw_meta"), row.get("occurred_at"))
    return {"id": row["id"], "source": row["source"], "title": row.get("title"), "date": row.get("occurred_at"),
            "snippet": row["snippet"] if row.get("snippet") is not None else snippet(row.get("body_text")),
            "open_url": t["open_url"], "open_label": t["open_label"], "local_file": bool(t["local_path"])}


CARD_COLS = """si.id, si.source, si.external_id, si.title, si.occurred_at, si.raw_meta,
               left(si.body_text, 600) AS body_text"""


def _roots() -> list[Path]:
    from igneous.sources import _fs_roots
    return _fs_roots()


@router.get("/items/{item_id}")
def get_item(item_id: int, s: Session = Depends(get_session)):
    r = s.execute(text("""SELECT id, source, external_id, title, body_text, occurred_at, raw_meta
                          FROM source_items WHERE id = :i"""), {"i": item_id}).mappings().first()
    if not r:
        raise HTTPException(404, "No such item")
    t = open_target(r["source"], r["external_id"], r["raw_meta"], r["occurred_at"])
    local = resolve_local(t["local_path"], _roots()) if t["local_path"] else None
    return {"id": r["id"], "source": r["source"], "title": r["title"], "occurred_at": r["occurred_at"],
            "text": r["body_text"] or "", "meta": safe_meta(r["raw_meta"]),
            "open_url": t["open_url"], "open_label": t["open_label"] if (t["open_url"] or local) else None,
            "local_path": str(local) if local else None}


@router.post("/items/{item_id}/open")
def open_item(item_id: int, s: Session = Depends(get_session)):
    """Open a local file in its default app. Only files under the configured roots; a local, read-only action."""
    r = s.execute(text("SELECT source, raw_meta FROM source_items WHERE id = :i"), {"i": item_id}).mappings().first()
    if not r or r["source"] not in LOCAL_SOURCES:
        raise HTTPException(404, "Not a local file")
    path = resolve_local((r["raw_meta"] or {}).get("path"), _roots())
    if path is None:
        raise HTTPException(404, "That file is gone or outside your configured folders")
    if sys.platform != "darwin":
        raise HTTPException(501, "Opening files is only supported on macOS")
    subprocess.run(["open", "--", str(path)], check=False, timeout=10)
    return {"opened": path.name}


@router.get("/graph/node/{node_id}/sources")
def node_sources(node_id: int, limit: int = Query(10, ge=1, le=50), offset: int = Query(0, ge=0),
                 s: Session = Depends(get_session)):
    """The items a node is made of, newest first: its own item (Message, File, Event), the items filed under a
    Project / Thread, or recent items shared with you for a Person. Falls back to the items its edges came from."""
    from igneous.graph.traversal import _canon_head, _my_ids
    n = s.execute(text("SELECT id, type FROM nodes WHERE id = :i"), {"i": node_id}).mappings().first()
    if not n:
        raise HTTPException(404)
    ids = _my_ids(s, _canon_head(s, node_id))
    params = {"ids": ids, "lim": limit + 1, "off": offset}
    if n["type"] == "Person":
        sql = f"""
            WITH me AS (SELECT id FROM nodes WHERE props->>'self' = 'true'
                        UNION SELECT node_id FROM canonical WHERE canonical_id IN
                            (SELECT id FROM nodes WHERE props->>'self' = 'true')),
            pm AS (SELECT src AS p, dst AS m FROM edges WHERE type = 'SENT'
                   UNION SELECT dst, src FROM edges WHERE type = 'RECEIVED'),
            shared AS (SELECT DISTINCT x.m FROM pm x WHERE x.p = ANY(:ids)
                       AND (NOT EXISTS (SELECT 1 FROM me)
                            OR EXISTS (SELECT 1 FROM pm y WHERE y.m = x.m AND y.p IN (SELECT id FROM me)))),
            sid AS (SELECT msg.source_item_id AS id FROM shared JOIN nodes msg ON msg.id = shared.m
                    UNION SELECT ev.source_item_id FROM edges a JOIN nodes ev ON ev.id = a.dst
                    WHERE a.type = 'ATTENDED' AND a.src = ANY(:ids))
            SELECT {CARD_COLS} FROM sid JOIN source_items si ON si.id = sid.id
            ORDER BY si.occurred_at DESC NULLS LAST, si.id LIMIT :lim OFFSET :off"""
    else:
        sql = f"""
            WITH sid AS (
                SELECT source_item_id AS id FROM nodes WHERE id = ANY(:ids)
                UNION SELECT m.source_item_id FROM edges e JOIN nodes m ON m.id = e.src
                    WHERE e.dst = ANY(:ids) AND e.type IN ('PART_OF', 'IN_THREAD', 'INVOLVED_IN', 'ATTACHED', 'CONTAINS')
                UNION SELECT e.source_item_id FROM edges e WHERE e.src = ANY(:ids) OR e.dst = ANY(:ids)
                UNION SELECT ee.source_item_id FROM edges e JOIN edge_evidence ee ON ee.edge_id = e.id
                    WHERE e.src = ANY(:ids) OR e.dst = ANY(:ids))
            SELECT {CARD_COLS} FROM sid JOIN source_items si ON si.id = sid.id
            ORDER BY si.occurred_at DESC NULLS LAST, si.id LIMIT :lim OFFSET :off"""
    rows = s.execute(text(sql), params).mappings().all()
    return {"items": [item_card(dict(r)) for r in rows[:limit]], "more": len(rows) > limit}
