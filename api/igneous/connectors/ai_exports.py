"""Chat exports from hosted assistants (ChatGPT, claude.ai, Gemini) as `ai_chats` day-items.

Same output as the local Claude Code/Codex connector (see ai_chats.day_items): your messages and the
assistant's prose, redacted, one item per conversation per day. Skipped: tool calls and results, system
messages, code-interpreter output, images and other non-text parts.

Accepted uploads: the export .zip as delivered, or the JSON file inside it.
  chatgpt    conversations.json (or conversations-000.json, ...): {title, create_time, mapping{...}}
  claude_ai  conversations.json: {uuid, name, created_at, chat_messages[...]}
  gemini     Google Takeout > My Activity > Gemini Apps, JSON format (the HTML format is not read)
Fields are read defensively: exports change shape without notice, so anything missing is skipped.
"""
import io
import json
import zipfile
from collections import defaultdict
from datetime import UTC, datetime
from typing import BinaryIO

from bs4 import BeautifulSoup

from igneous.connectors.ai_chats import _text_blocks, _ts, day_items
from igneous.connectors.base import SourceItem

PROVIDERS = ("chatgpt", "claude_ai", "gemini")

Conversation = tuple[dict, list[dict]]  # (session meta, turns)


class ExportError(ValueError):
    """The upload is not a readable export; the message is safe to show to the user."""


def _meta(title: str | None, session: str) -> dict:
    return {"title": title, "cwd": None, "branches": [], "session_id": session}


def chatgpt_conversations(data: list) -> list[Conversation]:
    out = []
    for i, c in enumerate(data):
        if not isinstance(c, dict) or not isinstance(c.get("mapping"), dict):
            continue
        turns = []
        for node in c["mapping"].values():
            m = (node or {}).get("message") or {}
            role = (m.get("author") or {}).get("role")
            content = m.get("content") or {}
            if role not in ("user", "assistant") or content.get("content_type") != "text":
                continue
            if (m.get("metadata") or {}).get("is_visually_hidden_from_conversation"):
                continue  # injected custom-instructions / memory context
            text = "\n".join(p for p in content.get("parts") or [] if isinstance(p, str) and p.strip())
            ts = m.get("create_time") or c.get("create_time")
            if text.strip() and isinstance(ts, int | float):
                turns.append({"at": datetime.fromtimestamp(ts, UTC), "role": "me" if role == "user" else "assistant",
                              "text": text})
        turns.sort(key=lambda t: t["at"])  # mapping is a tree keyed by id, not in order
        out.append((_meta(c.get("title"), str(c.get("conversation_id") or c.get("id") or f"chatgpt-{i}")), turns))
    return out


def claude_ai_conversations(data: list) -> list[Conversation]:
    out = []
    for i, c in enumerate(data):
        if not isinstance(c, dict) or not isinstance(c.get("chat_messages"), list):
            continue
        turns = []
        for m in c["chat_messages"]:
            role = m.get("sender")
            if role not in ("human", "assistant"):
                continue
            blocks = _text_blocks(m.get("content")) or _text_blocks(m.get("text"))  # content[] is newer than text
            text = "\n".join(b for b in blocks if b.strip())
            at = _ts(m.get("created_at"))
            if text.strip() and at:
                turns.append({"at": at, "role": "me" if role == "human" else "assistant", "text": text})
        out.append((_meta(c.get("name"), str(c.get("uuid") or f"claude-ai-{i}")), turns))
    return out


def _html_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    return soup.get_text("\n").strip()


def gemini_conversations(data: list) -> list[Conversation]:
    """Takeout has no conversation ids: a day's prompts become one session, like iMessage conversation-days."""
    by_day: dict[str, list[dict]] = defaultdict(list)
    for e in data:
        if not isinstance(e, dict):
            continue
        title, at = e.get("title"), _ts(e.get("time"))
        if not at or not isinstance(title, str) or not title.startswith("Prompted"):
            continue  # "Used an Assistant feature", "Gave feedback", etc.
        prompt = title.removeprefix("Prompted").strip()
        if not prompt:
            continue
        day = at.date().isoformat()
        by_day[day].append({"at": at, "role": "me", "text": prompt})
        reply = "\n".join(_html_text(h["html"]) for h in e.get("safeHtmlItem") or []
                          if isinstance(h, dict) and h.get("html"))
        if reply:
            by_day[day].append({"at": at, "role": "assistant", "text": reply})
    out = []
    for day, turns in sorted(by_day.items()):
        turns.sort(key=lambda t: (t["at"], t["role"] != "me"))  # Takeout lists newest first; prompt before its reply
        out.append((_meta("Gemini chats", f"gemini-{day}"), turns))
    return out


_PARSERS = {"chatgpt": chatgpt_conversations, "claude_ai": claude_ai_conversations, "gemini": gemini_conversations}


def detect(data) -> str | None:
    rows = [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []
    if any("mapping" in r for r in rows):
        return "chatgpt"
    if any("chat_messages" in r for r in rows):
        return "claude_ai"
    if any(r.get("header") == "Gemini Apps" or "safeHtmlItem" in r for r in rows):
        return "gemini"
    return None


def _json_members(zf: zipfile.ZipFile) -> list[str]:
    names = [n for n in zf.namelist() if n.lower().endswith(".json")]
    wanted = [n for n in names if "conversations" in n.lower().rsplit("/", 1)[-1]]
    wanted += [n for n in names if n not in wanted and "myactivity" in n.lower().replace(" ", "")]
    return wanted


def parse_export(fh: BinaryIO, provider: str | None = None) -> tuple[str, list[Conversation]]:
    """Return (provider, conversations) from an upload; raises ExportError with a readable reason."""
    if provider is not None and provider not in PROVIDERS:
        raise ExportError(f"Unknown provider {provider!r}; expected one of {', '.join(PROVIDERS)}.")
    if zipfile.is_zipfile(fh):
        with zipfile.ZipFile(fh) as zf:
            members = _json_members(zf)
            if not members:
                hint = (" The Gemini HTML format is not supported; re-export with JSON."
                        if any(n.lower().endswith(".html") for n in zf.namelist()) else "")
                raise ExportError("No conversations.json or Gemini MyActivity.json found in the zip." + hint)
            blobs = [zf.read(n) for n in members]
    else:
        fh.seek(0)
        blobs = [fh.read()]
    found: str | None = None
    convs: list[Conversation] = []
    for blob in blobs:
        try:
            data = json.load(io.BytesIO(blob))
        except (json.JSONDecodeError, UnicodeDecodeError):
            if blob.lstrip()[:1] == b"<":
                raise ExportError("This looks like HTML. Gemini exports must use the JSON format.") from None
            raise ExportError("The file is not valid JSON or a zip export.") from None
        kind = provider or detect(data)
        if kind is None or (found and kind != found):
            continue
        found = kind
        if isinstance(data, list):
            convs += _PARSERS[kind](data)
    if found is None:
        raise ExportError("Could not recognise this export. Expected a ChatGPT or claude.ai conversations.json, "
                          "or a Gemini Apps MyActivity.json from Google Takeout.")
    return found, convs


def export_items(provider: str, convs: list[Conversation]) -> list[SourceItem]:
    return [item for meta, turns in convs for item in day_items(provider, meta["session_id"], meta, turns)]


class ExportConnector:
    """Adapter so run_sync can upsert already-parsed items. No scan event, so it never prunes anything."""
    name = "ai_chats"

    def __init__(self, items: list[SourceItem]):
        self.items = items

    def sync(self, cursor: dict):
        yield from self.items
