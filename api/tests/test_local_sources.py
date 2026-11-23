"""Sources read from files on this computer: the Enron maildir, Messages, Contacts, Notes and AI coding chats."""
import gzip
import json
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text

from igneous.connectors import email_maildir
from igneous.connectors.ai_chats import AIChatConnector, claude_turns
from igneous.connectors.apple_notes import AppleNotesConnector, decode_body
from igneous.connectors.base import Checkpoint, CompletedScan, SourceItem, run_sync
from igneous.connectors.email_maildir import MaildirConnector
from igneous.connectors.imessage import APPLE_EPOCH, MessagesConnector, apple_time, decode_attributed
from igneous.connectors.mac_contacts import MacContactsConnector, norm_phone

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


def chat_db(path: Path) -> Path:
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE message (ROWID INTEGER PRIMARY KEY, date INTEGER, is_from_me INTEGER, text TEXT,
                              attributedBody BLOB, handle_id INTEGER, item_type INTEGER DEFAULT 0,
                              associated_message_type INTEGER DEFAULT 0, cache_has_attachments INTEGER DEFAULT 0);
        CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
        CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, chat_identifier TEXT, display_name TEXT, service_name TEXT,
                           style INTEGER);
        CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER);
        CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
        INSERT INTO chat VALUES (1, '+15555550100', '', 'iMessage', 45);
        INSERT INTO handle VALUES (1, '+15555550100');
        INSERT INTO chat_handle_join VALUES (1, 1);
    """)
    connection.commit()
    connection.close()
    return path


def text_message(path: Path, rowid: int, when: datetime, body: str) -> None:
    connection = sqlite3.connect(path)
    date = int((when - APPLE_EPOCH).total_seconds()) * 10**9
    connection.execute("INSERT INTO message (ROWID, date, is_from_me, text, handle_id) VALUES (?, ?, 0, ?, 1)",
                       (rowid, date, body))
    connection.execute("INSERT INTO chat_message_join VALUES (1, ?)", (rowid,))
    connection.commit()
    connection.close()


def test_a_message_that_arrives_late_is_read_whatever_its_date(tmp_path):
    db = chat_db(tmp_path / "chat.db")
    text_message(db, 1, datetime(2025, 6, 2, 12, tzinfo=UTC), "see you Tuesday")
    first = list(MessagesConnector(db).sync({}))
    assert [i.external_id for i in first if isinstance(i, SourceItem)] == ["+15555550100/2025-06-02"]
    assert first[-1].cursor == {"last_rowid": 1}
    # A message from the day before reaches this Mac after that sync (from the phone, or a restored backup).
    text_message(db, 2, datetime(2025, 6, 1, 9, tzinfo=UTC), "booked the table")
    late = list(MessagesConnector(db).sync(first[-1].cursor))
    (item,) = [i for i in late if isinstance(i, SourceItem)]
    assert item.external_id == "+15555550100/2025-06-01" and "booked the table" in item.body_text
    assert late[-1].cursor == {"last_rowid": 2}
    assert not [i for i in MessagesConnector(db).sync(late[-1].cursor) if isinstance(i, SourceItem)]


def test_phone_normalization():
    assert norm_phone("(614) 555-1234") == norm_phone("+1 614 555 1234") == norm_phone("6145551234") == "+16145551234"
    assert norm_phone("+44 20 7946 0958") == "+442079460958"
    assert norm_phone("555-1234") is None and norm_phone(None) is None


def contacts_db(path: Path, cards: list[tuple[str, str, str]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE ZABCDRECORD (Z_PK INTEGER, ZUNIQUEID TEXT, ZFIRSTNAME TEXT, ZLASTNAME TEXT, ZORGANIZATION TEXT);
        CREATE TABLE ZABCDPHONENUMBER (ZOWNER INTEGER, ZFULLNUMBER TEXT);
        CREATE TABLE ZABCDEMAILADDRESS (ZOWNER INTEGER, ZADDRESS TEXT);
    """)
    for pk, (uid, name, phone) in enumerate(cards, 1):
        connection.execute("INSERT INTO ZABCDRECORD VALUES (?, ?, ?, NULL, NULL)", (pk, uid, name))
        connection.execute("INSERT INTO ZABCDPHONENUMBER VALUES (?, ?)", (pk, phone))
    connection.commit()
    connection.close()
    return path


def test_a_deleted_contact_is_removed_and_a_hidden_address_book_removes_nothing(db, tmp_path):
    book = contacts_db(tmp_path / "Sources" / "A1" / "AddressBook-v22.abcddb",
                       [("card-1", "Sam", "(614) 555-0101"), ("card-2", "Kim", "614 555 0102")])
    pattern = str(tmp_path / "Sources" / "*" / "AddressBook-v22.abcddb")
    run_sync(db, MacContactsConnector(pattern))
    assert stored(db, "maccontacts") == ["A1/card-1", "A1/card-2"]
    connection = sqlite3.connect(book)
    connection.execute("DELETE FROM ZABCDRECORD WHERE ZUNIQUEID = 'card-2'")
    connection.commit()
    connection.close()
    assert run_sync(db, MacContactsConnector(pattern))["deleted"] == 1
    assert stored(db, "maccontacts") == ["A1/card-1"]
    # Without Full Disk Access the folder looks empty: that is an error, not every card deleted.
    with pytest.raises(PermissionError, match="Full Disk Access"):
        run_sync(db, MacContactsConnector(str(tmp_path / "Hidden" / "*" / "AddressBook-v22.abcddb")))
    assert stored(db, "maccontacts") == ["A1/card-1"]


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
    assert "Ship safely" in items[0].body_text
    assert next(event for event in events if isinstance(event, CompletedScan)).external_ids == frozenset({"note-1", "3"})


def test_a_notes_date_comes_from_the_column_its_macos_version_uses(tmp_path):
    """A note's dates carry a suffix in the shared object table (ZMODIFICATIONDATE1); without a date column a note
    has no date, never one made of its row number."""
    when = datetime(2025, 3, 9, 15, 30, tzinfo=UTC)
    seconds = (when - APPLE_EPOCH).total_seconds()
    for column, expected in (("ZMODIFICATIONDATE1", when), ("ZMODIFICATIONDATE", when), ("ZNOTEDATE", None)):
        path = tmp_path / f"{column}.sqlite"
        connection = sqlite3.connect(path)
        connection.executescript(f"""
            CREATE TABLE ZICCLOUDSYNCINGOBJECT (Z_PK INTEGER, ZTITLE1 TEXT, ZIDENTIFIER TEXT, {column} REAL);
            CREATE TABLE ZICNOTEDATA (ZNOTE INTEGER, ZDATA BLOB);
        """)
        connection.execute("INSERT INTO ZICCLOUDSYNCINGOBJECT VALUES (7, 'Plan', 'note-7', ?)", (seconds,))
        connection.execute("INSERT INTO ZICNOTEDATA VALUES (7, ?)", (gzip.compress(PLAN),))
        connection.commit()
        connection.close()
        (item,) = [e for e in AppleNotesConnector(path).sync({}) if isinstance(e, SourceItem)]
        assert item.occurred_at == expected, column


def test_a_notes_store_without_its_tables_is_an_error_not_every_note_deleted(db, tmp_path):
    path = notes_db(tmp_path / "NoteStore.sqlite", [(1, "Plan", "note-1", 0, PLAN)])
    run_sync(db, AppleNotesConnector(path))
    connection = sqlite3.connect(path)
    connection.execute("ALTER TABLE ZICNOTEDATA RENAME TO ZNOTEBODY")  # a layout this version doesn't know
    connection.commit()
    connection.close()
    with pytest.raises(RuntimeError, match="no notes tables"):
        run_sync(db, AppleNotesConnector(path))
    db.rollback()
    assert stored(db, "notes") == ["note-1"]


def test_the_mac_databases_are_closed_after_a_sync_and_a_blocked_one_says_why(db, tmp_path, monkeypatch):
    from igneous.connectors import macos

    chats = chat_db(tmp_path / "chat.db")
    text_message(chats, 1, datetime(2025, 6, 2, 12, tzinfo=UTC), "see you Tuesday")
    book = contacts_db(tmp_path / "Sources" / "A1" / "AddressBook-v22.abcddb", [("card-1", "Sam", "(614) 555-0101")])
    notes = notes_db(tmp_path / "NoteStore.sqlite", [(1, "Plan", "note-1", 0, PLAN)])
    opened = []
    connect = sqlite3.connect

    def recording(*args, **kwargs):
        opened.append(connect(*args, **kwargs))
        return opened[-1]

    monkeypatch.setattr(macos.sqlite3, "connect", recording)
    run_sync(db, MessagesConnector(chats))
    run_sync(db, MacContactsConnector(str(book)))
    run_sync(db, AppleNotesConnector(notes))
    sync = MessagesConnector(chats).sync({})
    next(sync)
    sync.close()  # a sync stopped early closes its database too
    assert len(opened) == 4
    for connection in opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")
    monkeypatch.setattr(macos.sqlite3, "connect", connect)

    blocked = tmp_path / "blocked.db"
    blocked.write_bytes(b"not a database")
    with pytest.raises(PermissionError, match="Full Disk Access"):
        list(AppleNotesConnector(blocked).sync({}))


def test_the_locked_flag_is_chosen_the_same_way_every_run(tmp_path):
    """ZPASSWORDHINT also mentions a password, but it is text, not the lock flag: whatever the order Python
    iterates the columns in (it varies per process), the locked note is skipped and the hinted one is read."""
    path = notes_db(tmp_path / "NoteStore.sqlite", [(1, "Open", "note-1", "a hint", 0, PLAN),
                                                    (2, "Locked", "note-2", None, 1, PLAN)],
                    columns="ZPASSWORDHINT TEXT, ZISPASSWORDPROTECTED INTEGER")
    code = ("import sqlite3, sys; from igneous.connectors.apple_notes import _note_query; "
            "print(_note_query(sqlite3.connect(sys.argv[1]))[4][4])")
    chosen = {subprocess.run([sys.executable, "-c", code, str(path)], capture_output=True, text=True, check=True,
                             env={**os.environ, "PYTHONHASHSEED": str(seed)}).stdout.strip() for seed in range(6)}
    assert chosen == {"ZISPASSWORDPROTECTED"}
    items = [e for e in AppleNotesConnector(path).sync({}) if isinstance(e, SourceItem)]
    assert [item.external_id for item in items] == ["note-1"]


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


def test_ai_chat_checkpoints_come_by_time_not_per_file(tmp_path, monkeypatch):
    from igneous.connectors import base

    proj = tmp_path / "claude" / "-Users-x-repo"
    proj.mkdir(parents=True)
    for n in range(4):
        (proj / f"s{n}.jsonl").write_text(json.dumps({"type": "user", "timestamp": "2026-10-21T10:00:00Z",
                                                      "message": {"content": f"session {n}"}}))
    conn = AIChatConnector(claude_root=str(tmp_path / "claude"), codex_root=str(tmp_path / "none"))
    checkpoints = [o for o in conn.sync({}) if isinstance(o, Checkpoint)]
    assert len(checkpoints) == 1 and len(checkpoints[0].cursor["files"]) == 4  # a quick scan writes its cursor once
    monkeypatch.setattr(base, "CHECKPOINT_SECONDS", 0)
    assert len([o for o in conn.sync({}) if isinstance(o, Checkpoint)]) == 5


def test_a_session_file_deleted_mid_sync_is_skipped_and_leaves_the_cursor(tmp_path, monkeypatch):
    """Claude Code and Codex delete old sessions on their own. One that goes between the listing and the read is
    skipped (its items stay), and a file that is gone leaves the cursor."""
    from igneous.connectors import ai_chats

    proj = tmp_path / "claude" / "-Users-x-repo"
    proj.mkdir(parents=True)
    for n in range(3):
        (proj / f"s{n}.jsonl").write_text(json.dumps({"type": "user", "timestamp": "2026-10-21T10:00:00Z",
                                                      "message": {"content": f"session {n}"}}))
    conn = AIChatConnector(claude_root=str(tmp_path / "claude"), codex_root=str(tmp_path / "none"))
    cursor = [o for o in conn.sync({}) if isinstance(o, Checkpoint)][-1].cursor
    (proj / "s3.jsonl").write_text(json.dumps({"type": "user", "timestamp": "2026-10-22T10:00:00Z",
                                               "message": {"content": "session 3"}}))
    listed = conn._files()
    (proj / "s0.jsonl").unlink()
    (proj / "s3.jsonl").unlink()  # gone between the listing and the read
    monkeypatch.setattr(ai_chats.AIChatConnector, "_files", lambda self: listed)
    events = list(conn.sync(cursor))
    assert not [o for o in events if isinstance(o, SourceItem)]
    assert sorted(os.path.basename(p) for p in events[-1].cursor["files"]) == ["s1.jsonl", "s2.jsonl"]


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
