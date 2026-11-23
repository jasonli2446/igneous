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


EVERY_KIND = {  # one synthetic credential per pattern
    "github_token": GH, "anthropic_key": "sk-ant-api03-" + "a" * 30, "openai_key": "sk-proj-" + "b" * 30,
    "aws_access_key": "AKIA" + "C" * 16, "bedrock_bearer": "ABSK" + "d" * 44, "google_api_key": "AIza" + "e" * 35,
    "google_oauth": "ya29." + "f" * 30, "google_client_secret": "GOCSPX-" + "g" * 24, "slack_token": "xoxb-" + "1" * 20,
    "stripe_key": "sk_live_" + "h" * 20, "jwt": "eyJ" + "i" * 12 + ".eyJ" + "j" * 12 + "." + "k" * 12,
    "private_key": "-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----",
    "url_credentials": "postgres://user:pw12345@db/x", "assigned_secret": 'DB_PASSWORD="supersecretvalue"',
}


def test_redaction_is_idempotent_for_every_pattern():
    once, counts = redact("\n".join(f"{kind}: {value}" for kind, value in EVERY_KIND.items()))
    assert set(counts) == {kind for kind, _ in PATTERNS}  # the sample covers every pattern
    twice, again = redact(once)
    assert twice == once and again == {}  # a placeholder never matches again: stored text redacts to itself


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


def test_metadata_text_is_redacted_at_any_depth_but_ids_and_paths_never_are():
    lookalike = "sk-proj-" + "a" * 24  # a folder, a message id or an address can look like a key
    meta = {
        "path": f"k1/{lookalike}/notes.md", "folders": ["k1", f"k1/{lookalike}"], "message_id": f"<{lookalike}@x>",
        "labels": [lookalike], "fingerprint": "f" * 40, "account": f"{lookalike}@x.com", "repo": lookalike,
        "from": [{"name": f"API_KEY={lookalike}", "email": f"{lookalike}@x.com"}],
        "attendees": [{"name": "Sam", "email": "sam@x.com", "response": f"see {GH}"}],
        "parent_names": {lookalike: f"Keys {GH}"},
        "boundaries": [{"kind": "heading", "title": f"Setup {GH}", "start": 0, "end": 4}],
        "description": {"nested": [[f"token: {GH}"]]},
    }
    item = stored(SourceItem("drive", f"me/{lookalike}", "t", "body", raw_meta=meta))
    out = item.raw_meta
    for key in ("path", "folders", "message_id", "labels", "fingerprint", "account", "repo"):
        assert out[key] == meta[key], key
    assert out["from"] == [{"name": "API_KEY=[REDACTED:openai_key]", "email": f"{lookalike}@x.com"}]
    assert out["attendees"][0]["response"] == "see [REDACTED:github_token]"
    assert out["parent_names"] == {lookalike: "Keys [REDACTED:github_token]"}  # a key is an id: kept
    assert out["boundaries"] == [{"kind": "heading", "title": "Setup [REDACTED:github_token]", "start": 0, "end": 4}]
    assert out["description"] == {"nested": [["token: [REDACTED:github_token]"]]}
    assert out["redactions"] == 5


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


# ---- items stored before the current rules ----

def old_row(s, source: str, external_id: str, title: str, body: str | None, meta: dict, content_hash: str = "old") -> int:
    """A row stored as read, under no version of the rules (version 0)."""
    return s.execute(text("""
        INSERT INTO source_items (source, external_id, content_hash, title, body_text, raw_meta, mime)
        VALUES (:s, :e, :h, :t, :b, CAST(:m AS jsonb), 'text/plain') RETURNING id
    """), {"s": source, "e": external_id, "h": content_hash, "t": title, "b": body, "m": json.dumps(meta)}).scalar_one()


def item_row(s, item_id: int) -> dict:
    return dict(s.execute(text("""SELECT title, body_text, raw_meta, content_hash, redaction_version, updated_at
                                   FROM source_items WHERE id = :i"""), {"i": item_id}).mappings().one())


def test_items_stored_before_the_rules_are_redacted_and_chunked_again(committed_db):
    from igneous.connectors.base import redact_stored
    from igneous.db import SessionLocal
    from igneous.graph import spine
    from igneous.rag import chunking

    page1, page2 = f"Deploy with {GH} today", "Second page"
    body = f"{page1}\n\n{page2}"
    bounds = [{"kind": "page", "index": 1, "start": 0, "end": len(page1)},
              {"kind": "page", "index": 2, "start": len(page1) + 2, "end": len(body)}]
    with SessionLocal() as s:
        item = old_row(s, "drive", "me/f1", f"Plan {GH}", body, {"boundaries": bounds, "drive_id": "f1",
                                                                 "path": "drive/f1", "parents": []})
        s.commit()
        spine.run(s)
        chunking.run(s)
        assert GH in s.execute(text("SELECT string_agg(text, ' ') FROM chunks")).scalar_one()
        s.execute(text("UPDATE chunks SET embedding = CAST(:v AS vector)"), {"v": json.dumps([0.0] * 384)})
        topic = s.execute(text("INSERT INTO nodes (type, key, name) VALUES ('Topic', 't', 't') RETURNING id")).scalar_one()
        file_node = s.execute(text("SELECT id FROM nodes WHERE source_item_id = :i"), {"i": item}).scalar_one()
        start = body.index(page2)
        s.execute(text("""INSERT INTO edges (src, dst, type, extractor, source_item_id, span_start, span_end)
                          VALUES (:a, :b, 'MENTIONS', 'llm', :i, :s, :e)"""),
                  {"a": file_node, "b": topic, "i": item, "s": start, "e": start + len(page2)})
        s.execute(text("DELETE FROM dirty_rebuild"))
        s.commit()
        before = item_row(s, item)

    with SessionLocal() as s:
        assert redact_stored(s) == {"checked": 1, "redacted": 1}
        after = item_row(s, item)
        text_, (b1, b2) = after["body_text"], after["raw_meta"]["boundaries"]
        assert after["title"] == "Plan [REDACTED:github_token]" and GH not in text_
        assert text_[b1["start"]:b1["end"]] == "Deploy with [REDACTED:github_token] today" and text_[b2["start"]:b2["end"]] == page2
        assert after["raw_meta"]["redactions"] == 2 and after["raw_meta"]["boundaries_redacted"] is True
        assert after["redaction_version"] == 1 and after["updated_at"] > before["updated_at"]
        span = s.execute(text("SELECT span_start, span_end FROM edges WHERE source_item_id = :i"), {"i": item}).one()
        assert text_[span.span_start:span.span_end] == page2  # the model's span still points at its words
        assert s.execute(text("SELECT name FROM nodes WHERE id = :n"), {"n": file_node}).scalar_one() == after["title"]
        assert s.execute(text("SELECT count(*) FROM dirty_rebuild")).scalar_one() == 1

        chunking.run(s)
        chunks = s.execute(text("SELECT text, embedding IS NULL AS fresh FROM chunks WHERE source_item_id = :i"),
                           {"i": item}).all()
        assert chunks and all(GH not in c.text and c.fresh for c in chunks)  # embed redoes them

        assert redact_stored(s) == {"checked": 0, "redacted": 0}  # nothing is out of date


def test_stored_metadata_is_redacted_in_place_too(db):
    from igneous.connectors.base import redact_stored

    event = old_row(db, "gcal", "me/team/e1", "Standup", "", {
        "calendar": "Team", "calendar_id": "team", "event_key": "gcal:e1@google.com",
        "attendees": [{"name": f"bot {GH}", "email": "bot@x.com"}]})
    before = item_row(db, event)
    assert redact_stored(db) == {"checked": 1, "redacted": 1}
    after = item_row(db, event)
    assert after["raw_meta"]["attendees"] == [{"name": "bot [REDACTED:github_token]", "email": "bot@x.com"}]
    assert after["raw_meta"]["event_key"] == "gcal:e1@google.com" and after["raw_meta"]["redactions"] == 1
    assert after["redaction_version"] == 1 and after["content_hash"] != before["content_hash"]


def test_a_new_version_of_the_rules_checks_every_item_again(db, monkeypatch):
    from igneous.connectors import redact as rules
    from igneous.connectors.base import redact_stored

    item = old_row(db, "gmail", "m1", "Hello", f"key {GH}", {"labels": []})
    assert redact_stored(db) == {"checked": 1, "redacted": 1}
    first = item_row(db, item)
    assert redact_stored(db) == {"checked": 0, "redacted": 0}
    monkeypatch.setattr(rules, "VERSION", 2)
    assert redact_stored(db) == {"checked": 1, "redacted": 0}  # redacted text stays as it is
    again = item_row(db, item)
    assert again["redaction_version"] == 2 and (again["body_text"], again["content_hash"]) == (
        first["body_text"], first["content_hash"])


def test_an_item_redacted_in_place_reads_as_unchanged_when_synced_again(db):
    from igneous.connectors.base import redact_stored

    leaky = SourceItem("gmail", "m1", "Hello", f"key {GH}", raw_meta={"labels": ["INBOX"]})
    clean = SourceItem("fs", "k/a.txt", "a.txt", "plain words", raw_meta={"path": "k/a.txt", "size": 11})
    old_row(db, "gmail", "m1", leaky.title, leaky.body_text, leaky.raw_meta, leaky.content_hash)
    clean_id = old_row(db, "fs", "k/a.txt", clean.title, clean.body_text, clean.raw_meta, "a-hash-of-the-file-bytes")
    unchanged_since = item_row(db, clean_id)["updated_at"]
    redact_stored(db)
    assert item_row(db, clean_id)["updated_at"] == unchanged_since  # nothing to redact: only stamped
    assert run_sync(db, Items("gmail", [leaky]))["unchanged"] == 1
    assert run_sync(db, Items("fs", [clean]))["unchanged"] == 1


def test_the_pipeline_redacts_before_anything_reads_the_items():
    from igneous.pipeline import stages

    assert list(stages())[:2] == ["redact", "spine"]


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
