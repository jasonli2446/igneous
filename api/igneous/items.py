"""Source items as the reader sees them: full text, safe metadata and where to open the original.

`GET /items/{id}` backs the in-app reader; `POST /items/{id}/open` opens a local file in its default macOS app;
`GET /graph/node/{id}` is one node with its own record (a message's email, a file's text), and
`GET /graph/node/{id}/sources` lists the records that make a node up. A record's metadata reaches a client only
through `safe_meta`. Read-only: nothing here writes to a source.
"""
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import Path as PathParam
from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous.db import get_session
from igneous.graph import identity

router = APIRouter()

# The record metadata a client may see. Anything else (tokens, cursors, file paths, headers, fingerprints) never
# leaves the API.
SAFE_META = ("from", "to", "cc", "calendar", "attendees", "organizer", "start", "end", "repo", "kind", "number",
             "author", "product", "chat", "group", "tool", "day", "labels", "original_mime", "modified_time")
LOCAL_SOURCES = ("fs",)
# Sources whose text names each speaker by a handle ("[14:39] +16174075277: Yes"), shown with contact names instead.
CHAT_TEXT = ("imessage",)


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
    """The part of a record's metadata a client may see (SAFE_META)."""
    return {k: v for k, v in (meta or {}).items() if k in SAFE_META}


def snippet(body: str | None, n: int = 160) -> str:
    return " ".join((body or "").split())[:n]


def _names(named: dict[str, str] | None, t: str | None) -> str | None:
    from igneous.graph.traversal import name_handles
    return name_handles(t, named) if named else t


def item_card(row: dict, named: dict[str, str] | None = None) -> dict:
    """The compact shape lists use (evidence, sources): enough to show, Open and View without another request. With
    `named` (traversal.names), a phone number in the snippet reads as its contact."""
    t = open_target(row["source"], row.get("external_id") or "", row.get("raw_meta"), row.get("occurred_at"))
    text_ = row["snippet"] if row.get("snippet") is not None else snippet(row.get("body_text"))
    return {"id": row["id"], "source": row["source"], "title": row.get("title"), "date": row.get("occurred_at"),
            "snippet": _names(named, text_), "open_url": t["open_url"], "open_label": t["open_label"],
            "local_file": bool(t["local_path"])}


CARD_COLS = """si.id, si.source, si.external_id, si.title, si.occurred_at, si.raw_meta,
               left(si.body_text, 600) AS body_text"""
ITEM_COLS = "id, source, external_id, title, body_text, occurred_at, raw_meta"


def _roots() -> list[Path]:
    from igneous.sources import fs_roots
    return fs_roots()


def item_view(row: dict, named: dict[str, str] | None = None) -> dict:
    """One record as the reader shows it: its whole text (a chat's handles named), safe metadata, and where the
    original opens."""
    t = open_target(row["source"], row["external_id"], row["raw_meta"], row["occurred_at"])
    local = resolve_local(t["local_path"], _roots()) if t["local_path"] else None
    body = row["body_text"] or ""
    return {"id": row["id"], "source": row["source"], "title": row["title"], "occurred_at": row["occurred_at"],
            "text": _names(named, body) if row["source"] in CHAT_TEXT else body, "meta": safe_meta(row["raw_meta"]),
            "open_url": t["open_url"], "open_label": t["open_label"] if (t["open_url"] or local) else None,
            "local_path": str(local) if local else None}


def node_detail(s: Session, node_id: int) -> dict:
    """A node as the inspector shows it (a person as their identity head), with its own record when it has one.
    Raises identity.NotFound."""
    from igneous.graph.traversal import category, label, names

    n = identity.node(s, node_id)
    named = names(s)
    item = None
    if n["source_item_id"]:
        row = s.execute(text(f"SELECT {ITEM_COLS} FROM source_items WHERE id = :i"),
                        {"i": n["source_item_id"]}).mappings().first()
        item = item_view(dict(row), named) if row else None
    props = n["props"] or {}
    return {"id": n["id"], "type": n["type"], "key": n["key"], "name": label(n["name"], n["type"], named),
            "category": category(n["type"], props.get("kind")), "props": props, "source_item": item}


def sources_of(s: Session, node_id: int, limit: int = 10, offset: int = 0) -> dict:
    """The records a node is made of, newest first: its own record (a message, file or event), the records filed under
    a project or conversation, or, for a person, the messages between you and them (all of their identities) and the
    events they attended; when they share nothing with you, their own. {items: [card], more}. Raises
    identity.NotFound."""
    from igneous.graph.traversal import names

    n = identity.node(s, node_id)
    ids = identity.members(s, n["id"])
    params = {"ids": ids, "lim": limit + 1, "off": offset}
    if n["type"] == "Person":
        owner = identity.self_id(s)
        mine = identity.members(s, owner) if owner is not None and owner != n["id"] else []
        sql = f"""
            WITH theirs AS (SELECT e.dst AS m FROM edges e WHERE e.src = ANY(:ids) AND e.type = 'SENT'
                            UNION SELECT e.src FROM edges e WHERE e.dst = ANY(:ids) AND e.type = 'RECEIVED'),
            shared AS (SELECT t.m FROM theirs t WHERE cardinality(CAST(:mine AS bigint[])) = 0
                       OR EXISTS (SELECT 1 FROM edges y WHERE y.dst = t.m AND y.type = 'SENT' AND y.src = ANY(:mine))
                       OR EXISTS (SELECT 1 FROM edges y WHERE y.src = t.m AND y.type = 'RECEIVED' AND y.dst = ANY(:mine))),
            sid AS (SELECT msg.source_item_id AS id FROM shared JOIN nodes msg ON msg.id = shared.m
                    UNION SELECT ev.source_item_id FROM edges a JOIN nodes ev ON ev.id = a.dst
                    WHERE a.type = 'ATTENDED' AND a.src = ANY(:ids))
            SELECT {CARD_COLS} FROM sid JOIN source_items si ON si.id = sid.id
            ORDER BY si.occurred_at DESC NULLS LAST, si.id LIMIT :lim OFFSET :off"""
        rows = s.execute(text(sql), {**params, "mine": mine}).mappings().all()
        if not rows and offset == 0 and mine:  # nothing shared with you: show their own records
            rows = s.execute(text(sql), {**params, "mine": []}).mappings().all()
    else:
        rows = s.execute(text(f"""
            WITH sid AS (
                SELECT source_item_id AS id FROM nodes WHERE id = ANY(:ids)
                UNION SELECT m.source_item_id FROM edges e JOIN nodes m ON m.id = e.src
                    WHERE e.dst = ANY(:ids) AND e.type IN ('PART_OF', 'IN_THREAD', 'INVOLVED_IN', 'ATTACHED', 'CONTAINS')
                UNION SELECT e.source_item_id FROM edges e WHERE e.src = ANY(:ids) OR e.dst = ANY(:ids)
                UNION SELECT ee.source_item_id FROM edges e JOIN edge_evidence ee ON ee.edge_id = e.id
                    WHERE e.src = ANY(:ids) OR e.dst = ANY(:ids))
            SELECT {CARD_COLS} FROM sid JOIN source_items si ON si.id = sid.id
            ORDER BY si.occurred_at DESC NULLS LAST, si.id LIMIT :lim OFFSET :off"""), params).mappings().all()
    named = names(s)
    return {"items": [item_card(dict(r), named) for r in rows[:limit]], "more": len(rows) > limit}


@router.get("/items/{item_id}")
def get_item(item_id: int = PathParam(ge=1), s: Session = Depends(get_session)):
    from igneous.graph.traversal import names

    r = s.execute(text(f"SELECT {ITEM_COLS} FROM source_items WHERE id = :i"), {"i": item_id}).mappings().first()
    if not r:
        raise HTTPException(404, "No such item")
    return item_view(dict(r), names(s))


@router.post("/items/{item_id}/open")
def open_item(item_id: int = PathParam(ge=1), s: Session = Depends(get_session)):
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


@router.get("/graph/node/{node_id}")
def graph_node(node_id: int = PathParam(ge=1), s: Session = Depends(get_session)):
    """A node as the inspector shows it, with its own record (text, safe metadata, where it opens) when it has one."""
    return node_detail(s, node_id)


@router.get("/graph/node/{node_id}/sources")
def node_sources(node_id: int = PathParam(ge=1), limit: int = Query(10, ge=1, le=50),
                 offset: int = Query(0, ge=0, le=10_000), s: Session = Depends(get_session)):
    """The records a node is made of, newest first (see `sources_of`)."""
    return sources_of(s, node_id, limit, offset)
