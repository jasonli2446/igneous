"""Exports you upload: the ChatGPT/Claude.ai/Gemini reader (connectors/ai_exports.py), the staged uploads
(uploads.py) and the import routes, which run as jobs. Synthetic exports only."""
import io
import json
import os
import time
import zipfile
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from igneous import jobs, uploads
from igneous.connectors.ai_exports import ExportError, conversations, export_items, parse_export
from igneous.db import SessionLocal
from igneous.main import app

TOKEN = "ghp_" + "A" * 36


def zipped(name: str, payload) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, json.dumps(payload))
    return buf.getvalue()


def export_items_of(payload, provider=None, zip_name=None):
    raw = zipped(zip_name, payload) if zip_name else json.dumps(payload).encode()
    kind, convs = parse_export(io.BytesIO(raw), provider)
    return kind, convs, export_items(kind, convs)


def ts(day, hour):
    return datetime(2025, 3, day, hour, tzinfo=UTC).timestamp()


def message(role, content_type, parts, at):
    return {"message": {"author": {"role": role}, "create_time": at, "content": {"content_type": content_type,
                                                                                  "parts": parts}}}


CHATGPT = [
    {"title": "SQL help", "create_time": ts(1, 9), "conversation_id": "cv1", "mapping": {
        "root": {"message": None},
        "c": message("assistant", "text", ["Use a partial index."], ts(1, 10)),  # out of order on purpose
        "a": message("system", "text", ["You are ChatGPT"], ts(1, 9)),
        "b": message("user", "text", [f"how do I speed up my query? token {TOKEN}"], ts(1, 9)),
        "d": message("tool", "text", ["TOOL OUTPUT"], ts(1, 10)),
        "e": message("assistant", "code", ["print('x')"], ts(1, 10)),
        "f": message("user", "multimodal_text", [{"asset_pointer": "x"}, "what is in this screenshot?"], ts(1, 10)),
        "g": message("user", "text", ["thanks, next day"], ts(2, 8)),
    }},
    {"title": "assistant only", "mapping": {"x": message("assistant", "text", ["hi"], ts(1, 9))}},
]


def test_chatgpt_export_keeps_text_drops_tools_and_groups_by_day():
    kind, convs, items = export_items_of(CHATGPT, zip_name="export/conversations.json")
    assert kind == "chatgpt" and len(convs) == 2
    assert [i.external_id for i in items] == ["chatgpt/cv1/2025-03-01", "chatgpt/cv1/2025-03-02"]
    body = items[0].body_text
    assert "me: how do I speed up" in body and "assistant: Use a partial index." in body
    assert body.index("how do I") < body.index("partial index")
    assert "what is in this screenshot?" in body  # a message with an image keeps its text
    for dropped in ("You are ChatGPT", "TOOL OUTPUT", "print(", "asset_pointer", TOKEN):
        assert dropped not in body
    assert "[REDACTED" in body and items[0].raw_meta["redactions"] == 1 and items[0].raw_meta["tool"] == "chatgpt"


def test_claude_ai_export_prefers_content_blocks_and_skips_empty():
    data = [{"uuid": "u1", "name": "Plan", "chat_messages": [
        {"sender": "human", "text": f"hello {TOKEN}", "created_at": "2025-04-01T10:00:00.000000Z",
         "content": [{"type": "text", "text": f"hello {TOKEN}"}]},
        {"sender": "assistant", "text": "", "created_at": "2025-04-01T10:00:05Z",
         "content": [{"type": "thinking", "thinking": "hmm"}, {"type": "text", "text": "Here is a plan."}]},
        {"sender": "human", "text": "legacy text only", "created_at": "2025-04-02T09:00:00Z"},
    ]}, {"uuid": "u2", "name": "none", "chat_messages": [
        {"sender": "assistant", "text": "orphan", "created_at": "2025-04-01T10:00:00Z"}]}]
    kind, _, items = export_items_of(data)
    assert kind == "claude_ai"
    assert [i.external_id for i in items] == ["claude_ai/u1/2025-04-01", "claude_ai/u1/2025-04-02"]
    assert "assistant: Here is a plan." in items[0].body_text and "hmm" not in items[0].body_text
    assert TOKEN not in items[0].body_text and items[0].raw_meta["redactions"] == 1
    assert "legacy text only" in items[1].body_text


def test_gemini_takeout_groups_a_day_and_strips_html():
    data = [
        {"header": "Gemini Apps", "title": "Prompted second question", "time": "2025-05-01T15:00:00.000Z",
         "safeHtmlItem": [{"html": "<p>Answer <b>two</b></p><ul><li>a</li></ul>"}]},
        {"header": "Gemini Apps", "title": "Prompted first question", "time": "2025-05-01T14:00:00.000Z",
         "safeHtmlItem": [{"html": "<p>Answer one</p>"}]},
        {"header": "Gemini Apps", "title": "Used an Assistant feature", "time": "2025-05-01T16:00:00.000Z"},
        {"header": "Gemini Apps", "title": "Prompted next day", "time": "2025-05-02T08:00:00.000Z"},
    ]
    kind, convs, items = export_items_of(data, zip_name="Takeout/My Activity/Gemini Apps/MyActivity.json")
    assert kind == "gemini" and len(convs) == 2
    assert [i.external_id for i in items] == ["gemini/gemini-2025-05-01/2025-05-01",
                                              "gemini/gemini-2025-05-02/2025-05-02"]
    body = items[0].body_text
    assert body.index("first question") < body.index("Answer one") < body.index("second question")
    assert "<p>" not in body and "Answer" in body and "Assistant feature" not in body


def test_export_rejects_unknown_and_html_uploads():
    for raw in (b'{"a": 1}', b"<html></html>", b"[]"):
        with pytest.raises(ExportError):
            parse_export(io.BytesIO(raw))
    with pytest.raises(ExportError, match="looks like HTML"):
        parse_export(io.BytesIO(b"  <html><body>My Activity</body></html>"))


def test_an_export_is_read_one_conversation_at_a_time(tmp_path):
    good = json.dumps(CHATGPT[0])
    export = tmp_path / "conversations.json"
    export.write_text(f"[{good}, {{\"title\": \"cut")  # the second conversation is cut short
    read = conversations(export)
    kind, (meta, _) = next(read)  # the first arrives before the rest of the file is read
    assert kind == "chatgpt" and meta["session_id"] == "cv1"
    with pytest.raises(ExportError, match="stops partway"):
        next(read)


# ---- the routes: staged uploads, background jobs ----

def wait(job_id: str) -> dict:
    for _ in range(200):
        with SessionLocal() as s:
            job = jobs.get(s, job_id)
        if job["status"] != "running":
            return job
        time.sleep(0.05)
    raise AssertionError("job never finished")


def staged_folders() -> set:
    return set(uploads.ROOT.glob("*")) if uploads.ROOT.is_dir() else set()


def test_an_ai_export_upload_imports_in_the_background_and_its_copy_goes(committed_db):
    before = staged_folders()
    response = TestClient(app).post("/import/ai-export", files={"file": ("chatgpt.zip", zipped(
        "conversations.json", CHATGPT), "application/zip")})
    assert response.status_code == 200, response.text
    job = response.json()
    assert job["kind"] == "import" and job["source"] == "chatgpt"
    done = wait(job["id"])
    assert done["status"] == "succeeded", done["error"]
    assert done["result"]["provider"] == "chatgpt" and done["result"]["conversations"] == 2
    assert done["result"]["inserted"] == 2 and done["result"]["redactions"] == 1
    assert staged_folders() == before  # the job removed its upload


def damaged_zip() -> bytes:
    """A zip whose end record is intact (is_zipfile says yes) but whose central directory is not."""
    data = bytearray(zipped("Takeout/My Activity/Search/MyActivity.json", []))
    cd = data.rfind(b"PK\x01\x02")
    data[cd:cd + 4] = b"XXXX"
    return bytes(data)


def test_a_bad_upload_is_refused_and_leaves_nothing_behind(committed_db):
    before = staged_folders()
    client = TestClient(app, raise_server_exceptions=False)
    for content in (b"PK\x03\x04 not really a zip", damaged_zip()):
        for route in ("/import/takeout", "/import/linkedin", "/import/ai-export", "/import/vcard"):
            response = client.post(route, files={"file": ("export.zip", content, "application/zip")})
            assert response.status_code == 422, (route, response.text)
    assert staged_folders() == before


def test_an_import_that_cannot_start_removes_its_upload(committed_db, monkeypatch):
    before = staged_folders()

    def busy(*args, **kwargs):
        raise jobs.JobBusy("chatgpt is already running")

    monkeypatch.setattr(jobs, "start", busy)
    response = TestClient(app).post("/import/ai-export", files={"file": ("c.json", json.dumps(CHATGPT).encode(),
                                                                            "application/json")})
    assert response.status_code == 409 and staged_folders() == before


def test_uploads_are_copied_off_the_event_loop(monkeypatch):
    calls = []
    real = uploads.run_in_threadpool

    async def spy(fn, *args):
        calls.append(fn.__name__)
        return await real(fn, *args)

    monkeypatch.setattr(uploads, "run_in_threadpool", spy)
    TestClient(app).post("/import/ai-export", files={"file": ("x.json", b"[]", "application/json")})
    assert calls == ["_copy"]


def test_folders_of_processes_that_died_are_swept(tmp_path, monkeypatch):
    monkeypatch.setattr(uploads, "ROOT", tmp_path)
    gone = tmp_path / "999999999-takeout-aa"  # no such pid
    earlier_me = tmp_path / f"{os.getpid()}-linkedin-bb"  # this pid, but not this process's
    parent = tmp_path / f"{os.getppid()}-ai-export-cc"  # alive
    for folder in (gone, earlier_me, parent):
        folder.mkdir()
    assert uploads.sweep() == 2
    assert [p.name for p in tmp_path.iterdir()] == [parent.name]


def test_the_cli_and_the_route_store_the_same_items(committed_db, tmp_path):
    from typer.testing import CliRunner

    from igneous.cli import app as cli

    export = tmp_path / "chatgpt.zip"
    export.write_bytes(zipped("conversations.json", CHATGPT))
    assert CliRunner().invoke(cli, ["import", "ai-export", str(export)]).exit_code == 0
    with SessionLocal() as s:
        first = s.execute(text("SELECT external_id, content_hash FROM source_items ORDER BY 1")).all()
    job = TestClient(app).post("/import/ai-export", json={"path": str(export)}).json()
    assert wait(job["id"])["result"]["unchanged"] == 2
    with SessionLocal() as s:
        assert s.execute(text("SELECT external_id, content_hash FROM source_items ORDER BY 1")).all() == first
