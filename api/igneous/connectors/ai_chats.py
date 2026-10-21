"""AI coding-assistant transcripts (Claude Code, Codex) as a source.

These conversations record what you built, the decisions you made, and the things you said you'd do, often
with no email or chat trail at all. A Claude Code session also says which repo it ran in (`cwd`) and on which
branch, which is a direct link from the conversation to a project.

What is kept, and what isn't:
  kept     your messages (including ones sent mid-turn) and the assistant's prose replies
  skipped  tool calls, tool results, file dumps, thinking, images, harness/system records, and subagent
           transcripts (the parent session already carries their outcome)
Every kept string passes through redact() first: transcripts contain pasted tokens and connection strings.

One SourceItem per session per day (like iMessage conversation-days), so a session that runs for days
doesn't become one blob, and a re-sync only re-emits the days that changed. The cursor is each file's
mtime and size: unchanged files are skipped without being read.
"""
import glob
import json
import os
from collections import defaultdict
from collections.abc import Iterator
from datetime import datetime

from igneous.connectors.base import Checkpoint, SourceItem
from igneous.connectors.redact import redact

CLAUDE_ROOT = os.path.expanduser("~/.claude/projects")
CODEX_ROOT = os.path.expanduser("~/.codex/sessions")
MAX_TURN_CHARS = 6000  # a single pasted wall of text shouldn't swamp a day's item


def _ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _text_blocks(content) -> list[str]:
    """Only prose: plain strings and `text` blocks. Never tool_use / tool_result / thinking / images."""
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [b["text"] for b in content if isinstance(b, dict) and b.get("type") == "text" and b.get("text")]
    return []


def claude_turns(path: str) -> tuple[dict, list[dict]]:
    """Parse one Claude Code session file into (session meta, turns). A turn is {at, role, text}."""
    meta: dict = {"title": None, "cwd": None, "branches": set(), "session_id": None}
    turns: list[dict] = []
    with open(path, errors="replace") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = r.get("type")
            if t in ("ai-title", "custom-title"):
                # The user's own title wins over the generated one.
                title = r.get("customTitle") or r.get("aiTitle")
                if title and (t == "custom-title" or not meta["title"]):
                    meta["title"] = title
                continue
            meta["session_id"] = meta["session_id"] or r.get("sessionId")
            if r.get("cwd"):
                meta["cwd"] = meta["cwd"] or r["cwd"]
            if r.get("gitBranch"):
                meta["branches"].add(r["gitBranch"])
            at = _ts(r.get("timestamp"))
            if t in ("user", "assistant"):
                if r.get("isMeta") or r.get("isSidechain") or r.get("isCompactSummary"):
                    continue  # system-injected context, subagent turns, auto-summaries
                for txt in _text_blocks((r.get("message") or {}).get("content")):
                    turns.append({"at": at, "role": "me" if t == "user" else "assistant", "text": txt})
            elif t == "attachment":
                a = r.get("attachment") or {}
                # Messages typed while the assistant was working arrive as queued commands, not user records.
                # The prompt is a string, or a list of content blocks when it carried an image: keep the text only.
                if a.get("type") == "queued_command" and a.get("prompt"):
                    for txt in _text_blocks(a["prompt"]):
                        turns.append({"at": _ts(a.get("timestamp")) or at, "role": "me", "text": txt})
    meta["branches"] = sorted(meta["branches"])
    return meta, turns


def codex_turns(path: str) -> tuple[dict, list[dict]]:
    """Codex rollout files: session_meta (cwd) plus response_item messages with input_text/output_text."""
    meta: dict = {"title": None, "cwd": None, "branches": [], "session_id": None}
    turns: list[dict] = []
    with open(path, errors="replace") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            p = r.get("payload") or {}
            if r.get("type") == "session_meta":
                meta["cwd"] = p.get("cwd")
                meta["session_id"] = p.get("id")
                if (p.get("git") or {}).get("branch"):
                    meta["branches"] = [p["git"]["branch"]]
                continue
            if r.get("type") != "response_item" or p.get("type") != "message":
                continue
            role = p.get("role")
            if role not in ("user", "assistant"):
                continue
            for b in p.get("content") or []:
                if isinstance(b, dict) and b.get("type") in ("input_text", "output_text") and b.get("text"):
                    txt = b["text"]
                    if role == "user" and txt.lstrip().startswith(("<environment_context", "<user_instructions")):
                        continue  # harness-injected context, not something the user wrote
                    turns.append({"at": _ts(r.get("timestamp")), "role": "me" if role == "user" else "assistant",
                                  "text": txt})
    return meta, turns


class AIChatConnector:
    """Claude Code and Codex sessions on this machine. Read-only (CO-2)."""
    name = "ai_chats"

    def __init__(self, claude_root: str = CLAUDE_ROOT, codex_root: str = CODEX_ROOT):
        self.claude_root, self.codex_root = claude_root, codex_root

    def _files(self) -> list[tuple[str, str]]:
        # Top-level session files only: */subagents/*.jsonl are agent-to-agent tool work.
        files = [("claude_code", p) for p in glob.glob(os.path.join(self.claude_root, "*", "*.jsonl"))]
        files += [("codex", p) for p in glob.glob(os.path.join(self.codex_root, "**", "*.jsonl"), recursive=True)]
        return sorted(files, key=lambda f: f[1])

    def sync(self, cursor: dict) -> Iterator[SourceItem | Checkpoint]:
        seen = dict(cursor.get("files", {}))
        for tool, path in self._files():
            st = os.stat(path)
            sig = f"{int(st.st_mtime)}:{st.st_size}"
            if seen.get(path) == sig:
                continue  # unchanged since last sync: don't even read it
            meta, turns = (claude_turns if tool == "claude_code" else codex_turns)(path)
            by_day: dict[str, list[dict]] = defaultdict(list)
            for t in turns:
                if t["at"]:
                    by_day[t["at"].date().isoformat()].append(t)
            session = meta["session_id"] or os.path.splitext(os.path.basename(path))[0]
            for day, day_turns in sorted(by_day.items()):
                lines, secrets = [], 0
                for t in day_turns:
                    clean, found = redact(t["text"][:MAX_TURN_CHARS])
                    secrets += sum(found.values())
                    lines.append(f"[{t['at']:%H:%M}] {t['role']}: {clean.strip()}")
                if not any(t["role"] == "me" for t in day_turns):
                    continue  # a day with only assistant output (e.g. a resumed tail) has nothing of yours
                title, _ = redact(meta["title"] or "")
                yield SourceItem(
                    source="ai_chats", external_id=f"{tool}/{session}/{day}",
                    title=f"{title or 'AI session'} ({tool.replace('_', ' ')}, {day})",
                    body_text="\n".join(lines), occurred_at=day_turns[0]["at"], mime="text/plain",
                    raw_meta={"tool": tool, "session": session, "day": day, "cwd": meta["cwd"],
                              "repo": os.path.basename(meta["cwd"].rstrip("/")) if meta["cwd"] else None,
                              "branches": meta["branches"], "turns": len(day_turns),
                              "my_turns": sum(1 for t in day_turns if t["role"] == "me"),
                              "redactions": secrets})
            seen[path] = sig
            yield Checkpoint({"files": seen})
        yield Checkpoint({"files": seen})
