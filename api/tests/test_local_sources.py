"""Sources read from files on this computer: the Enron maildir, Messages, Contacts, Notes and AI coding chats."""
import gzip
import json
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import text

from igneous.connectors import email_maildir
from igneous.connectors.ai_chats import AIChatConnector, claude_turns
from igneous.connectors.apple_notes import AppleNotesConnector, decode_body
from igneous.connectors.base import CompletedScan, SourceItem, run_sync
from igneous.connectors.email_maildir import MaildirConnector
from igneous.connectors.imessage import apple_time, decode_attributed
from igneous.connectors.mac_contacts import norm_phone

MAIL = "Message-ID: <{0}@x>\nFrom: a@enron.com\nTo: b@enron.com\nSubject: {0}\n\nbody {0}\n"


def maildir(root: Path, *paths: str) -> Path:
    for rel in paths:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(MAIL.format(rel))
    return root


def stored(s, source: str) -> list[str]:
    return sorted(s.execute(text("SELECT external_id FROM source_items WHERE source = :s"), {"s": source}).scalars())


def test_each_mailbox_keeps_its_own_place(db, tmp_path):
    root = maildir(tmp_path, "allen-p/inbox/1.", "allen-p/inbox/2.", "lay-k/inbox/1.")
    run_sync(db, MaildirConnector(root, ["lay-k"]))
    run_sync(db, MaildirConnector(root, ["allen-p"]))  # sorts before lay-k's position: still read in full
    assert stored(db, "email") == ["allen-p/inbox/1.", "allen-p/inbox/2.", "lay-k/inbox/1."]
    cursor = db.execute(text("SELECT cursor FROM sync_state WHERE connector = 'email'")).scalar_one()
    assert cursor == {"mailboxes": {"allen-p": "allen-p/inbox/2.", "lay-k": "lay-k/inbox/1."}}


def test_a_shared_position_holds_for_its_own_mailbox(tmp_path):
    root = maildir(tmp_path, "allen-p/inbox/1.", "allen-p/inbox/2.", "lay-k/inbox/1.")
    events = list(MaildirConnector(root).sync({"last_path": "allen-p/inbox/1."}))
    assert [e.external_id for e in events if hasattr(e, "external_id")] == ["allen-p/inbox/2.", "lay-k/inbox/1."]


def test_a_resumed_mailbox_skips_nothing_whatever_the_folder_names(db, tmp_path, monkeypatch):
    monkeypatch.setattr(email_maildir, "CHECKPOINT_EVERY", 1)
    root = maildir(tmp_path, "x/a/1.", "x/a-b/1.")  # as Paths "a" sorts before "a-b"; as strings after it
    run_sync(db, MaildirConnector(root, ["x"]), limit=1)
    run_sync(db, MaildirConnector(root, ["x"]))
    assert stored(db, "email") == ["x/a-b/1.", "x/a/1."]


def test_an_unknown_mailbox_is_an_error(tmp_path):
    root = maildir(tmp_path, "allen-p/inbox/1.")
    with pytest.raises(ValueError, match="No mailbox nobody-x"):
        list(MaildirConnector(root, ["nobody-x"]).sync({}))


# ---- Messages and Contacts ----

def test_imessage_decoding_and_dates():
    short = b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x12NSAttributedString\x00\x84\x84\x08NSObject" \
            b"\x00\x85\x92\x84\x84\x84\x08NSString\x01\x94\x84\x01+\x05hello\x86"
    assert decode_attributed(short) == "hello"
    long_text = "x" * 300
    long_blob = b"NSString\x01\x94\x84\x01+\x81" + (300).to_bytes(2, "little") + long_text.encode()
    assert decode_attributed(long_blob) == long_text
    assert decode_attributed(None) is None and decode_attributed(b"garbage") is None
    assert apple_time(0) is None
    assert apple_time(700_000_000 * 10**9).year == 2023   # nanoseconds (current format)
    assert apple_time(700_000_000).year == 2023           # seconds (pre-10.13 rows)


def test_phone_normalization():
    assert norm_phone("(614) 555-1234") == norm_phone("+1 614 555 1234") == norm_phone("6145551234") == "+16145551234"
    assert norm_phone("+44 20 7946 0958") == "+442079460958"
    assert norm_phone("555-1234") is None and norm_phone(None) is None


# ---- Apple Notes ----

def notes_db(path: Path, rows: list[tuple], columns: str = "ZISPASSWORDPROTECTED INTEGER") -> Path:
    connection = sqlite3.connect(path)
    connection.executescript(f"""
        CREATE TABLE ZICCLOUDSYNCINGOBJECT (Z_PK INTEGER, ZTITLE1 TEXT, ZIDENTIFIER TEXT, ZMODIFICATIONDATE REAL,
                                            {columns});
        CREATE TABLE ZICNOTEDATA (ZNOTE INTEGER, ZDATA BLOB);
    """)
    for pk, title, identifier, *rest in rows:
        body, locked = rest[-1], rest[:-1]
        connection.execute(f"INSERT INTO ZICCLOUDSYNCINGOBJECT VALUES (?, ?, ?, 1{', ?' * len(locked)})",
                           (pk, title, identifier, *locked))
        connection.execute("INSERT INTO ZICNOTEDATA VALUES (?, ?)", (pk, gzip.compress(body)))
    connection.commit()
    connection.close()
    return path


PLAN = b"\x0a\x0cProject plan\x12\x0bShip safely"


def test_apple_notes_gzip_protobuf_body_and_locked_rows(tmp_path):
    assert "Project plan" in decode_body(gzip.compress(PLAN))
    db = notes_db(tmp_path / "NoteStore.sqlite", [(1, "Plan", "note-1", 0, PLAN), (2, "Locked", "note-2", 1, PLAN),
                                                   (3, "Fallback", None, 0, PLAN)])
    events = list(AppleNotesConnector(db).sync({}))
    items = [event for event in events if isinstance(event, SourceItem)]
    assert [(item.external_id, item.title) for item in items] == [("note-1", "Plan"), ("3", "Fallback")]
    assert "Ship safely" in items[0].body_text and items[0].raw_meta["model_opt_in"] is False
    assert next(event for event in events if isinstance(event, CompletedScan)).external_ids == frozenset({"note-1", "3"})


def test_a_notes_title_is_stored_redacted_and_a_rename_is_a_change(db, tmp_path):
    token = "ghp_" + "N" * 36
    path = notes_db(tmp_path / "NoteStore.sqlite", [(1, f"deploy key {token}", "note-1", 0, PLAN)])
    run_sync(db, AppleNotesConnector(path))
    assert db.execute(text("SELECT title FROM source_items WHERE source = 'notes'")).scalar_one() == \
        "deploy key [REDACTED:github_token]"
    connection = sqlite3.connect(path)
    connection.execute("UPDATE ZICCLOUDSYNCINGOBJECT SET ZTITLE1 = 'Deploy notes'")
    connection.commit()
    connection.close()
    assert run_sync(db, AppleNotesConnector(path))["updated"] == 1


# ---- AI coding chats ----

def test_ai_chat_keeps_prose_drops_tools_and_redacts(tmp_path):
    """Synthetic Claude Code session: prose kept, tools/thinking/meta/subagents dropped, secrets redacted."""
    proj = tmp_path / "claude" / "-Users-x-repo"
    (proj / "sess1" / "subagents").mkdir(parents=True)
    token = "ghp_" + "Q" * 36
    rows = [
        {"type": "custom-title", "customTitle": "Igneous chat connector"},
        {"type": "user", "sessionId": "sess1", "cwd": "/Users/x/igneous", "gitBranch": "feat/chats",
         "timestamp": "2026-10-21T10:00:00Z", "message": {"content": f"build the connector, token {token}"}},
        {"type": "assistant", "sessionId": "sess1", "timestamp": "2026-10-21T10:01:00Z", "message": {"content": [
            {"type": "thinking", "thinking": "SECRET_THOUGHT"},
            {"type": "text", "text": "Plan: parse jsonl."},
            {"type": "tool_use", "input": {"command": "cat .env"}}]}},
        {"type": "user", "timestamp": "2026-10-21T10:02:00Z",
         "message": {"content": [{"type": "tool_result", "content": "DB_PASSWORD=hunter2hunter2"}]}},
        {"type": "user", "isMeta": True, "timestamp": "2026-10-21T10:03:00Z", "message": {"content": "<system note>"}},
        {"type": "attachment", "timestamp": "2026-10-21T10:04:00Z",
         "attachment": {"type": "queued_command", "prompt": "also redact secrets"}},
        {"type": "attachment", "timestamp": "2026-10-21T10:05:00Z", "attachment": {"type": "environment", "snapshot": {}}},
        {"type": "user", "timestamp": "2026-10-22T09:00:00Z", "message": {"content": "next day"}},
    ]
    (proj / "sess1.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    (proj / "sess1" / "subagents" / "agent-1.jsonl").write_text(json.dumps(
        {"type": "user", "timestamp": "2026-10-21T10:00:00Z", "message": {"content": "SUBAGENT_TEXT"}}))

    conn = AIChatConnector(claude_root=str(tmp_path / "claude"), codex_root=str(tmp_path / "none"))
    items = [o for o in conn.sync({}) if isinstance(o, SourceItem)]
    assert [i.raw_meta["day"] for i in items] == ["2026-10-21", "2026-10-22"]  # one item per session-day
    day1 = items[0]
    assert "Plan: parse jsonl." in day1.body_text and "also redact secrets" in day1.body_text
    for leaked in (token, "SECRET_THOUGHT", "cat .env", "hunter2hunter2", "<system note>", "SUBAGENT_TEXT"):
        assert leaked not in day1.body_text, leaked
    assert "[REDACTED:github_token]" in day1.body_text and day1.raw_meta["redactions"] == 1
    assert day1.raw_meta["repo"] == "igneous" and day1.raw_meta["branches"] == ["feat/chats"]
    assert day1.title.startswith("Igneous chat connector")

    # Unchanged files are skipped on the next sync (cursor = mtime + size).
    cursor = [o for o in conn.sync({}) if not isinstance(o, SourceItem)][-1].cursor
    assert not [o for o in conn.sync(cursor) if isinstance(o, SourceItem)]


def test_queued_prompts_with_images_keep_their_text(tmp_path):
    f = tmp_path / "s.jsonl"
    f.write_text(json.dumps({"type": "attachment", "timestamp": "2026-10-21T10:00:00Z", "attachment": {
        "type": "queued_command", "prompt": [{"type": "text", "text": "see this screenshot"}, {"type": "image"}]}}))
    _, turns = claude_turns(str(f))
    assert [t["text"] for t in turns] == ["see this screenshot"]


def test_ai_session_repo_comes_from_files_touched_not_launch_dir(tmp_path):
    repo = tmp_path / "work" / "igneous"
    (repo / ".git").mkdir(parents=True)
    (repo / "api").mkdir()
    f = tmp_path / "s.jsonl"
    rows = [
        {"type": "user", "cwd": str(tmp_path), "timestamp": "2026-10-21T10:00:00Z",
         "message": {"content": "fix the api"}},
        {"type": "assistant", "timestamp": "2026-10-21T10:01:00Z", "message": {"content": [
            {"type": "tool_use", "name": "Edit", "input": {"file_path": str(repo / "api" / "main.py")}},
            {"type": "tool_use", "name": "Bash", "input": {"command": f"cd {repo}/api && pytest"}}]}},
    ]
    f.write_text("\n".join(json.dumps(r) for r in rows))
    meta, _ = claude_turns(str(f))
    assert meta["repo_path"] == str(repo)
