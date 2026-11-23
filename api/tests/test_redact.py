"""Credentials never reach storage: the patterns (connectors/redact.py) and the sync runner that applies them to
every item it stores."""
import importlib.util
import json

from conftest import API
from sqlalchemy import text

from igneous.connectors import ai_chats
from igneous.connectors.base import SourceItem, run_sync, stored
from igneous.connectors.redact import PATTERNS, redact, url_without_credentials

GH = "ghp_" + "A" * 36  # synthetic credentials in real formats; none of these are live


class Items:
    def __init__(self, name: str, items: list[SourceItem]):
        self.name, self.items = name, items

    def sync(self, cursor):
        yield from self.items


def row(s, source: str) -> dict:
    return dict(s.execute(text("SELECT title, body_text, raw_meta, content_hash FROM source_items WHERE source = :s"),
                          {"s": source}).mappings().one())


def leaks(*values) -> list[str]:
    """Every credential pattern match left in the given strings."""
    return [m.group(0) for v in values for _, p in PATTERNS for m in p.finditer(v or "") if "[REDACTED" not in m.group(0)]


def test_redact_secrets():
    fake = {"gh": GH, "ant": "sk-ant-api03-" + "b" * 40, "aws": "AKIA" + "Z" * 16, "goog": "AIza" + "c" * 35,
            "jwt": "eyJ" + "a" * 12 + ".eyJ" + "b" * 12 + "." + "c" * 12}
    sample = (f"use {fake['gh']} and {fake['ant']}, aws {fake['aws']}, key {fake['goog']}, jwt {fake['jwt']}\n"
              "DATABASE_URL=postgresql://igneous:hunter2pass@db:5432/x\nOPENAI_API_KEY=abcdefgh12345678\n"
              "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----")
    out, counts = redact(sample)
    for v in fake.values():
        assert v not in out
    assert "hunter2pass" not in out and "abcdefgh12345678" not in out and "MIIEow" not in out
    assert "OPENAI_API_KEY=[REDACTED:assigned_secret]" in out  # the name survives, the value doesn't
    assert counts["github_token"] == 1 and counts["private_key"] == 1
    # Ordinary prose is untouched.
    assert redact("the token bucket refills at 600 units per minute")[0] == "the token bucket refills at 600 units per minute"
    assert redact(None) == (None, {})


def test_json_style_keys_are_matched():
    out, counts = redact('{"api_key": "abcd1234efgh", "password":"hunter2hunter2", \\"client_secret\\": \\"s3cr3tvalue\\"}')
    assert "abcd1234efgh" not in out and "hunter2hunter2" not in out and "s3cr3tvalue" not in out
    assert '"api_key": "[REDACTED:assigned_secret]"' in out and counts == {"assigned_secret": 3}


def test_redaction_is_idempotent():
    once, _ = redact('DB_PASSWORD=supersecretvalue and postgres://u:pw12345@h/db and "token": "abcdefgh12345"')
    twice, counts = redact(once)
    assert twice == once and counts == {}  # placeholders aren't re-matched (a scan of stored text reads 0)


def test_a_remote_loses_its_user_name_and_token():
    assert url_without_credentials("https://jane:" + GH + "@github.com/a/b.git") == "https://github.com/a/b.git"
    assert url_without_credentials(" https://oauth2:abc123secret@gitlab.com/a/b.git") == "https://gitlab.com/a/b.git"
    assert url_without_credentials("git@github.com:a/b.git") == "git@github.com:a/b.git"  # scp-style: no secret
    assert url_without_credentials("https://github.com/a/b.git?token=" + GH) == \
        "https://github.com/a/b.git?token=[REDACTED:github_token]"


def test_every_item_is_stored_redacted_whatever_its_connector(db):
    """Gmail, Calendar, Drive, GitHub, Messages and the others don't redact on their own: the runner does."""
    item = SourceItem("gmail", "m1", f"Your key {GH}", f"Hi,\nAWS_SECRET_ACCESS_KEY=abcdefghijklmnop\n{GH}\n",
                      raw_meta={"labels": ["INBOX"]})
    stats = run_sync(db, Items("gmail", [item]))
    stored_row = row(db, "gmail")
    assert not leaks(stored_row["title"], stored_row["body_text"])
    assert stored_row["raw_meta"]["redactions"] == 3 and stats["redactions"] == 3
    # The hash is the stored content's, so syncing the same message again changes nothing.
    assert stored_row["content_hash"] == stored(item).content_hash != item.content_hash
    assert run_sync(db, Items("gmail", [item]))["unchanged"] == 1


def test_an_item_without_secrets_keeps_its_hash_and_metadata(db):
    item = SourceItem("email", "a/1", "Forecast", "Here is our forecast.", raw_meta={"from": []})
    run_sync(db, Items("email", [item]))
    assert row(db, "email")["raw_meta"] == {"from": []} and row(db, "email")["content_hash"] == item.content_hash


def test_boundaries_move_with_redactions_and_removed_nul_characters(db):
    page1, page2 = f"key {GH} here\x00", "page two"
    body = f"{page1}\n\n{page2}"
    bounds = [{"kind": "page", "index": 1, "start": 0, "end": len(page1)},
              {"kind": "page", "index": 2, "start": len(page1) + 2, "end": len(body)}]
    run_sync(db, Items("drive", [SourceItem("drive", "me/f", "Plan.pdf", body, raw_meta={"boundaries": bounds})]))
    stored_row = row(db, "drive")
    text_, (b1, b2) = stored_row["body_text"], stored_row["raw_meta"]["boundaries"]
    assert "\x00" not in text_ and GH not in text_
    assert text_[b1["start"]:b1["end"]] == "key [REDACTED:github_token] here" and text_[b2["start"]:b2["end"]] == page2
    assert stored_row["raw_meta"]["boundaries_redacted"] is True


def test_nul_characters_are_removed_from_metadata_too(db):
    run_sync(db, Items("fs", [SourceItem("fs", "k/a", "a\x00.txt", None, raw_meta={"note": "x\x00y", "list": ["\x00"]})]))
    stored_row = row(db, "fs")
    assert stored_row["title"] == "a.txt" and stored_row["raw_meta"] == {"note": "xy", "list": [""]}


def test_a_long_ai_chat_turn_is_redacted_before_it_is_shortened():
    from datetime import UTC, datetime

    at = datetime(2026, 1, 1, tzinfo=UTC)
    secret = "sk-ant-api03-" + "k" * 60
    turn = "x" * (ai_chats.MAX_TURN_CHARS - 20) + " " + secret  # the cut falls inside the key
    (item,) = ai_chats.day_items("claude_code", "s1", {"title": None, "cwd": None, "branches": []},
                                 [{"at": at, "role": "me", "text": turn}])
    assert "sk-ant" not in item.body_text and item.raw_meta["redactions"] == 1


def test_the_remote_scrub_migration_strips_stored_credentials(db):
    spec = importlib.util.spec_from_file_location(
        "m0009", API / "migrations" / "versions" / "0009_remote_credentials_and_job_owners.py")
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    leaky, plain = f"https://jane:{GH}@github.com/a/b.git", "git@github.com:a/c.git"
    for i, remote in enumerate((leaky, plain)):
        db.execute(text("INSERT INTO source_items (source, external_id, content_hash, raw_meta) "
                        "VALUES ('fs', :i, 'h', CAST(:m AS jsonb))"), {"i": f"k/.repos/{i}", "m": json.dumps({"remote": remote})})
        db.execute(text("INSERT INTO nodes (type, key, name, props) VALUES ('Folder', :k, 'r', CAST(:p AS jsonb))"),
                   {"k": f"repo:{i}", "p": json.dumps({"remote": remote, "kind": "repo"})})
    db.execute(text(migration.SCRUB))
    items = db.execute(text("SELECT raw_meta->>'remote' FROM source_items ORDER BY external_id")).scalars().all()
    nodes = db.execute(text("SELECT props->>'remote' FROM nodes ORDER BY key")).scalars().all()
    assert items == nodes == ["https://github.com/a/b.git", plain]
