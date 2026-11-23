"""Chat exports from hosted assistants (ChatGPT, claude.ai, Gemini) as `ai_chats` day-items.

Same output as the local Claude Code/Codex connector (see ai_chats.day_items): your messages and the
assistant's prose, redacted, one item per conversation per day. Skipped: tool calls and results, system
messages, code-interpreter output, images and other non-text parts.

Accepted files: the export .zip as delivered, or the JSON file inside it.
  chatgpt    conversations.json (or conversations-000.json, ...): {title, create_time, mapping{...}}
  claude_ai  conversations.json: {uuid, name, created_at, chat_messages[...]}
  gemini     Google Takeout > My Activity > Gemini Apps, JSON format (the HTML format is not read)
An export is read one conversation at a time (ijson), so its size doesn't matter; a day of Gemini prompts is a
conversation, so Gemini's are gathered per file. Fields are read defensively: exports change shape without notice,
so anything missing is skipped.
"""
import zipfile
from collections import defaultdict
from collections.abc import Callable, Iterator
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

import ijson
from bs4 import BeautifulSoup

from igneous.connectors.ai_chats import _text_blocks, _ts, day_items
from igneous.connectors.base import SourceItem

PROVIDERS = ("chatgpt", "claude_ai", "gemini")

Conversation = tuple[dict, list[dict]]  # (session meta, turns)
Export = Path | BinaryIO  # a file on disk, or an open binary file


class ExportError(ValueError):
    """The upload is not a readable export; the message is safe to show to the user."""


def _meta(title: str | None, session: str) -> dict:
    return {"title": title, "cwd": None, "branches": [], "session_id": session}


def chatgpt_conversation(c, i: int) -> Conversation | None:
    """One ChatGPT conversation (the i-th of its file). Text and multimodal messages keep their text parts; the
    images in a multimodal message are skipped."""
    if not isinstance(c, dict) or not isinstance(c.get("mapping"), dict):
        return None
    turns = []
    for node in c["mapping"].values():
        m = (node or {}).get("message") or {}
        role = (m.get("author") or {}).get("role")
        content = m.get("content") or {}
        if role not in ("user", "assistant") or content.get("content_type") not in ("text", "multimodal_text"):
            continue
        if (m.get("metadata") or {}).get("is_visually_hidden_from_conversation"):
            continue  # injected custom-instructions / memory context
        text = "\n".join(p for p in content.get("parts") or [] if isinstance(p, str) and p.strip())
        ts = m.get("create_time") or c.get("create_time")
        if text.strip() and isinstance(ts, int | float):
            turns.append({"at": datetime.fromtimestamp(float(ts), UTC),
                          "role": "me" if role == "user" else "assistant", "text": text})
    turns.sort(key=lambda t: t["at"])  # mapping is a tree keyed by id, not in order
    return _meta(c.get("title"), str(c.get("conversation_id") or c.get("id") or f"chatgpt-{i}")), turns


def claude_ai_conversation(c, i: int) -> Conversation | None:
    if not isinstance(c, dict) or not isinstance(c.get("chat_messages"), list):
        return None
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
    return _meta(c.get("name"), str(c.get("uuid") or f"claude-ai-{i}")), turns


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


def detect(record) -> str | None:
    """The provider whose export a record of a JSON file's top-level list comes from."""
    if not isinstance(record, dict):
        return None
    if "mapping" in record:
        return "chatgpt"
    if "chat_messages" in record:
        return "claude_ai"
    if record.get("header") == "Gemini Apps" or "safeHtmlItem" in record:
        return "gemini"
    return None


def _json_members(zf: zipfile.ZipFile) -> list[str]:
    names = [n for n in zf.namelist() if n.lower().endswith(".json")]
    wanted = [n for n in names if "conversations" in n.lower().rsplit("/", 1)[-1]]
    wanted += [n for n in names if n not in wanted and "myactivity" in n.lower().replace(" ", "")]
    return wanted


def _documents(export: Export) -> Iterator[Callable]:
    """An opener for each JSON document of an export: the conversation and Gemini activity files of a zip, or the
    file itself."""
    if zipfile.is_zipfile(export):
        with zipfile.ZipFile(export) as zf:
            members = _json_members(zf)
            if not members:
                hint = (" The Gemini HTML format is not supported; re-export with JSON."
                        if any(n.lower().endswith(".html") for n in zf.namelist()) else "")
                raise ExportError("No conversations.json or Gemini MyActivity.json found in the zip." + hint)
            for name in members:
                yield lambda name=name: zf.open(name)
        return
    if isinstance(export, Path):
        yield lambda: open(export, "rb")
    else:
        yield lambda: _rewound(export)


def _rewound(fh: BinaryIO):
    """An open file from its start, left open after use (its owner closes it)."""
    fh.seek(0)
    return nullcontext(fh)


def _records(open_document: Callable) -> Iterator:
    """The items of a JSON document's top-level list, one at a time."""
    seen = False
    with open_document() as fh:
        try:
            for record in ijson.items(fh, "item", use_float=True):
                seen = True
                yield record
        except (ijson.JSONError, UnicodeDecodeError) as e:
            if seen:
                raise ExportError("The export stops partway: the file is cut short or damaged. Download it again.") from e
            with open_document() as again:
                head = again.read(256).lstrip()
            if head[:1] == b"<":
                raise ExportError("This looks like HTML. Gemini exports must use the JSON format.") from None
            raise ExportError("The file is not valid JSON or a zip export.") from None


def conversations(export: Export, provider: str | None = None) -> Iterator[tuple[str, Conversation]]:
    """(provider, conversation) for each conversation of an export, read one at a time. The provider is
    recognised from the first record of each document unless given; a document from another provider than the
    first is skipped. Raises ExportError with a reason the user can act on."""
    if provider is not None and provider not in PROVIDERS:
        raise ExportError(f"Unknown provider {provider!r}; expected one of {', '.join(PROVIDERS)}.")
    found = provider
    for open_document in _documents(export):
        kind, gemini = provider, []
        for i, record in enumerate(_records(open_document)):
            kind = kind or detect(record)
            if kind is None:
                continue
            if found and kind != found:
                break
            found = kind
            if kind == "gemini":
                gemini.append(record)  # a day's prompts can be anywhere in the file
            elif conv := (chatgpt_conversation if kind == "chatgpt" else claude_ai_conversation)(record, i):
                yield kind, conv
        for conv in gemini_conversations(gemini):
            yield "gemini", conv
    if found is None:
        raise ExportError("Could not recognise this export. Expected a ChatGPT or claude.ai conversations.json, "
                          "or a Gemini Apps MyActivity.json from Google Takeout.")


def preflight(export: Export, provider: str | None = None) -> str:
    """The export's provider, from its first recognisable record: a cheap check before an import starts. Raises
    ExportError when the file is no export."""
    if provider is not None and provider not in PROVIDERS:
        raise ExportError(f"Unknown provider {provider!r}; expected one of {', '.join(PROVIDERS)}.")
    for open_document in _documents(export):
        for record in _records(open_document):
            if kind := provider or detect(record):
                return kind
    raise ExportError("Could not recognise this export. Expected a ChatGPT or claude.ai conversations.json, "
                      "or a Gemini Apps MyActivity.json from Google Takeout.")


def parse_export(fh: BinaryIO, provider: str | None = None) -> tuple[str, list[Conversation]]:
    """(provider, conversations) of an export already in memory."""
    found = [(kind, conv) for kind, conv in conversations(fh, provider)]
    return found[0][0] if found else preflight(fh, provider), [conv for _, conv in found]


def export_items(provider: str, convs: list[Conversation]) -> list[SourceItem]:
    return [item for meta, turns in convs for item in day_items(provider, meta["session_id"], meta, turns)]


class ExportConnector:
    """An export's day-items for run_sync, read as they are stored. No checkpoint and no scan event: an import
    keeps no cursor and never deletes anything."""
    name = "ai_chats"

    def __init__(self, export: Export, provider: str, progress: Callable[..., None] | None = None):
        self.export, self.provider, self.progress = export, provider, progress
        self.conversations = 0

    def sync(self, cursor: dict) -> Iterator[SourceItem]:
        for kind, (meta, turns) in conversations(self.export, self.provider):
            self.conversations += 1
            yield from day_items(kind, meta["session_id"], meta, turns)
            if self.progress:
                self.progress({"conversations": self.conversations})


def run_import(s, path: Path, provider: str | None = None, progress: Callable[..., None] | None = None) -> dict:
    """Import one export file: the body of `igneous import ai-export` and of the POST /import/ai-export job.
    Re-running it changes nothing: ids are stable and unchanged items keep their content hash."""
    from igneous.connectors.base import run_sync

    kind = preflight(path, provider)
    connector = ExportConnector(path, kind, progress)
    stats = run_sync(s, connector, account=kind)
    return {"provider": kind, "conversations": connector.conversations, **stats}
