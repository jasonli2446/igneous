from datetime import datetime

import pytest

from igneous.connectors.email_maildir import parse_message
from igneous.graph.spine import _name_from, _thread_key
from igneous.rag.chunking import split, strip_quoted
from igneous.rag.search import rrf

RAW = b"""Message-ID: <123.JavaMail.evans@thyme>
Date: Mon, 14 May 2001 16:39:00 -0700 (PDT)
From: phillip.allen@enron.com
To: tim.belden@enron.com, John.Arnold@enron.com
Subject: Re: Forecast
In-Reply-To: <100.JavaMail.evans@thyme>
X-From: Phillip K Allen
X-Folder: \\Phillip_Allen_Jan2002_1\\Allen, Phillip K.\\'Sent Mail

Here is our forecast.

 -----Original Message-----
From: Belden, Tim
Sent: Monday
Old quoted text.
"""


def test_synthetic_cross_source_corpus_is_deterministic_and_valid(tmp_path):
    from igneous.synthetic_corpus import NOTICE, generate, verify

    first = tmp_path / "first"
    second = tmp_path / "second"
    assert generate(first) == generate(second)
    assert verify(first) == {"documents": 5, "calendar_events": 2}
    assert NOTICE in (first / "README.md").read_text()
    assert NOTICE in (first / "calendar.ics").read_text()
    assert "phillip.allen@enron.com" in (first / "files/2001-05-14-forecast-memo.md").read_text()


def test_parse_message():
    item = parse_message(RAW, "allen-p/sent/1.")
    m = item.raw_meta
    assert m["from"] == [{"name": "", "email": "phillip.allen@enron.com"}]
    assert [a["email"] for a in m["to"]] == ["tim.belden@enron.com", "john.arnold@enron.com"]
    assert m["in_reply_to"] == "<100.JavaMail.evans@thyme>"
    assert item.title == "Re: Forecast"
    assert item.occurred_at.year == 2001


def test_content_hash_stable():
    assert parse_message(RAW, "a").content_hash == parse_message(RAW, "a").content_hash


def test_strip_quoted():
    assert strip_quoted(parse_message(RAW, "a").body_text) == "Here is our forecast."


def test_split_long_text_overlaps_and_covers():
    t = ("word " * 1000).strip()
    parts = split(t)
    assert len(parts) > 1 and all(len(p) <= 2000 for p in parts)
    assert split("short") == ["short"] and split("") == []


def test_names_and_threads():
    assert _name_from({"name": "", "email": "phillip.allen@enron.com"}, "Phillip K Allen") == "Phillip K Allen"
    assert _name_from({"name": "", "email": "tim.belden@enron.com"}, None) == "Tim Belden"
    assert _thread_key({"references": ["<a>", "<b>"]}, "x") == "<a>"
    assert _thread_key({"from": [{"email": "a@x"}], "to": [{"email": "b@x"}]}, "RE: FW: Forecast") == "subject:forecast|a@x,b@x"


def test_rrf_rewards_agreement():
    assert rrf([1, 2, 3], [3, 2, 9])[0][0] in (2, 3)
    assert {c for c, _ in rrf([1], [2])} == {1, 2}


def test_graph_filters_scope_discovery_and_final_serialization():
    """A source/date request must not discover or serialize a non-matching edge."""
    from igneous.graph.traversal import GraphFilters, neighborhood

    class Result:
        def __init__(self, rows):
            self.rows = rows

        def scalars(self):
            return self

        def all(self):
            return self.rows

        def mappings(self):
            return self

    class Session:
        def __init__(self):
            self.calls = []

        def execute(self, statement, params):
            self.calls.append((str(statement), params))
            sql = str(statement)
            if "SELECT CASE WHEN e.src" in sql:
                return Result([])  # zero discovered neighbors
            if "FROM nodes n" in sql:
                return Result([{"id": 7, "type": "Person", "name": "Seed", "key": "seed", "degree": 3}])
            return Result([])

    session = Session()
    graph = neighborhood(session, [7], filters=GraphFilters(source="gmail", edge_types=["SENT"], since=datetime(2024, 1, 1)))
    discovery, serialized = session.calls[0], session.calls[-1]
    for sql, params in (discovery, serialized):
        assert "LEFT JOIN source_items si ON si.id = e.source_item_id" in sql
        assert "si.source = :source" in sql
        assert "COALESCE(si.occurred_at, e.valid_from) >= :since" in sql
        assert "e.type = ANY(:types)" in sql
        assert params["source"] == "gmail" and params["types"] == ["SENT"]
    assert graph == {"nodes": [{"id": 7, "type": "Person", "name": "Seed", "key": "seed", "degree": 3}], "edges": []}


def test_locate_spans():
    from igneous.graph.extract import locate

    body = "Please ask Jeff\n  Skilling and FERC about the caps."
    assert locate(body, "FERC") == (body.index("FERC"), body.index("FERC") + 4)
    s0, s1 = locate(body, "Jeff Skilling")  # survives the line wrap
    assert body[s0:s1].split() == ["Jeff", "Skilling"]
    assert locate(body, "Ken Lay") is None and locate(body, "  ") is None


def test_extraction_spans_index_the_stored_body():
    """Regression: offsets were computed on a .strip()-ed body and stored against the original (off by one)."""
    from igneous.graph.extract import locate
    from igneous.rag.chunking import QUOTE_START

    stored = "\n  Ask David Oxley.\n -----Original Message-----\nold"
    own = stored[: QUOTE_START.search(stored).start()]
    s0, s1 = locate(own, "David Oxley")
    assert stored[s0:s1] == "David Oxley"


def test_er_names_and_scoring():
    from igneous.graph.resolve import P, same_first, score, split_name, unresolvable

    assert split_name("Allen, Phillip K.", "pallen@enron.com") == ("phillip", "allen")
    assert split_name("", "jeff.skilling@enron.com") == ("jeff", "skilling")
    assert same_first("rick", "richard") and same_first("joe", "joseph") and not same_first("ann", "mark")

    def p(i, key, name, peers=()):
        f, last = split_name(name, key)
        return P(i, key, name, f, last, set(peers))

    shared = {10, 11, 12, 13}
    assert score(p(1, "james.steffes@enron.com", "James Steffes", shared), p(2, "steffes@enron.com", "James Steffes", shared))[0] >= 0.75
    assert score(p(1, "ken.lay@enron.com", "Ken Lay"), p(2, "ken_lay@enron.net", "Ken Lay"))[0] >= 0.75
    # Same surname, different first name: never merges no matter how much mail they share.
    assert score(p(1, "ann.schmidt@enron.com", "Ann Schmidt", shared), p(2, "mark.schmidt@enron.com", "Mark Schmidt", shared))[0] == 0
    assert unresolvable(p(3, ".john@enron.com", "e-mail")) and unresolvable(p(4, "dl-ga-all@enron.com", "DL GA All"))


def test_er_rejects_mailing_lists():
    from igneous.graph.resolve import P, split_name, unresolvable

    for key, name in [("aei-bi_updates@www.brookings.org", "Aei-bi Updates"),
                      ("western.price.survey.contacts@proxy4.ba.best.com", "Western Price Survey Contacts")]:
        f, last = split_name(name, key)
        assert unresolvable(P(1, key, name, f, last, set()))


def test_imessage_decoding_and_dates():
    from igneous.connectors.imessage import apple_time, decode_attributed

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


def test_quoted_header_parsing():
    from igneous.graph.quoted import _surname, parse_quotes

    body = ("Sounds good.\n\n -----Original Message-----\nFrom: Rice, Ken \nSent: Mon 8/20/2001 10:20 AM \n"
            "To: Skilling, Jeff \nSubject: RE: Astros tickets\n\nThanks for the tickets.")
    (q,) = parse_quotes(body)
    assert (q["sent"].month, q["sent"].day, q["sent"].hour, q["sent"].minute) == (8, 20, 10, 20)
    assert q["subject"] == "Astros tickets" and _surname(q["from"]) == "rice"
    from igneous.graph.quoted import _first
    assert _first("Rice, Ken") == "ken" and _first("Brad Schaeffer <b@x.com>") == "brad" and _first("Rice") == ""
    long = "-----Original Message-----\nFrom: Brad Schaeffer <BSchaeffer@prebon.com>@ENRON \nSent: Thursday, November 29, 2001 9:49 AM\nSubject: x\n"
    assert parse_quotes(long)[0]["sent"].year == 2001 and _surname(parse_quotes(long)[0]["from"]) == "schaeffer"
    assert parse_quotes("no quote here") == []


def test_parse_plain_text_markdown_and_binary_are_nonfatal():
    from igneous.parsers import parse_document

    markdown = parse_document(b"# Title\r\n\r\nBody", "notes.md")
    assert (markdown.status, markdown.mime, markdown.parser, markdown.text) == (
        "ok", "text/markdown", "text", "# Title\n\nBody")
    unsupported = parse_document(b"\x89PNG\r\n\x1a\n", "image.png")
    assert unsupported.status == "unsupported" and unsupported.text == "" and unsupported.error
    binary = parse_document(b"\x00\x01", "bad.txt")
    assert binary.status == "binary" and binary.text == "" and binary.error


def test_parse_pdf_with_page_boundaries():
    from igneous.parsers import parse_document

    def pdf_bytes():
        objects = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R 5 0 R] /Count 2 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 7 0 R >> >> >>",
            b"<< /Length 42 >>\nstream\nBT /F1 12 Tf 72 720 Td (First page) Tj ET\nendstream",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 6 0 R /Resources << /Font << /F1 7 0 R >> >> >>",
            b"<< /Length 43 >>\nstream\nBT /F1 12 Tf 72 720 Td (Second page) Tj ET\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        ]
        out, offsets = bytearray(b"%PDF-1.4\n"), [0]
        for number, obj in enumerate(objects, start=1):
            offsets.append(len(out))
            out.extend(f"{number} 0 obj\n".encode() + obj + b"\nendobj\n")
        xref = len(out)
        out.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
        out.extend(b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:]))
        out.extend(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode())
        return bytes(out)

    parsed = parse_document(pdf_bytes(), "ignored.bin")
    assert parsed.status == "ok" and parsed.parser == "pdf"
    assert parsed.text == "First page\n\nSecond page"
    assert parsed.boundaries == [
        {"kind": "page", "index": 1, "start": 0, "end": 10},
        {"kind": "page", "index": 2, "start": 12, "end": 23},
    ]


def test_parse_docx_xlsx_and_pptx_structures():
    from io import BytesIO

    from docx import Document
    from openpyxl import Workbook
    from pptx import Presentation

    from igneous.parsers import parse_document

    docx = Document()
    docx.add_heading("Plan", level=1)
    docx.add_paragraph("Ship it")
    data = BytesIO()
    docx.save(data)
    parsed = parse_document(data.getvalue(), "plan.docx")
    assert parsed.status == "ok" and parsed.text == "Plan\nShip it"
    assert parsed.boundaries == [{"kind": "heading", "level": 1, "title": "Plan", "start": 0, "end": 4}]

    workbook = Workbook()
    workbook.active.title = "Numbers"
    workbook.active.append(["name", "value"])
    workbook.active.append(["one", 1])
    workbook.create_sheet("Empty")
    data = BytesIO()
    workbook.save(data)
    parsed = parse_document(data.getvalue(), "data.xlsx")
    assert parsed.status == "ok" and parsed.text == "name\tvalue\none\t1\n\n"
    assert parsed.boundaries == [
        {"kind": "sheet", "index": 1, "name": "Numbers", "start": 0, "end": 16},
        {"kind": "sheet", "index": 2, "name": "Empty", "start": 18, "end": 18},
    ]

    presentation = Presentation()
    presentation.slides.add_slide(presentation.slide_layouts[5]).shapes.add_textbox(0, 0, 1, 1).text = "Hello slide"
    data = BytesIO()
    presentation.save(data)
    parsed = parse_document(data.getvalue(), "deck.pptx")
    assert parsed.status == "ok" and parsed.text == "Hello slide"
    assert parsed.boundaries == [{"kind": "slide", "index": 1, "start": 0, "end": 11}]


def test_parse_corrupt_office_file_is_nonfatal():
    from igneous.parsers import parse_document

    parsed = parse_document(b"not a document", "broken.docx")
    assert parsed.status == "corrupt" and parsed.parser == "docx" and parsed.text == "" and parsed.error


def test_gmail_backfill_resumes_mid_page(monkeypatch):
    """Regression: a mid-page checkpoint saved only the page token, so resuming re-fetched the page."""
    from igneous.connectors import gmail
    from igneous.connectors.base import Checkpoint

    monkeypatch.setattr(gmail, "CHECKPOINT_EVERY", 2)
    monkeypatch.setattr(gmail, "_retry", lambda call, units=5, tries=6: call)
    fetched = []

    class Conn(gmail.GmailConnector):
        def __init__(self):
            self.query, self.since_days, self.account = "", None, "x"
            pages = {None: {"messages": [{"id": f"m{i}"} for i in range(5)], "nextPageToken": "p2"},
                     "p2": {"messages": [{"id": "m5"}]}}

            class Svc:
                def users(_):
                    class U:
                        def getProfile(_, userId):
                            return {"historyId": "h1"}

                        def messages(_):
                            class M:
                                def list(_, pageToken=None, **k):
                                    return pages[pageToken]
                            return M()
                    return U()
            self.svc = Svc()

        def _fetch(self, msg_id):
            fetched.append(msg_id)
            return None

    out = list(Conn()._backfill())
    mid = [c.cursor for c in out if isinstance(c, Checkpoint) and "backfill_offset" in c.cursor]
    assert mid[1] == {"history_id": "h1", "backfill_page": None, "backfill_offset": 4, "backfill_q": ""}
    fetched.clear()
    list(Conn()._backfill(mid[1]))
    assert fetched == ["m4", "m5"]  # resumes after m3, not from m0


def test_phone_normalization():
    from igneous.connectors.mac_contacts import norm_phone

    assert norm_phone("(614) 555-1234") == norm_phone("+1 614 555 1234") == norm_phone("6145551234") == "+16145551234"
    assert norm_phone("+44 20 7946 0958") == "+442079460958"
    assert norm_phone("555-1234") is None and norm_phone(None) is None


def test_contact_names_in_messages():
    from igneous.graph.traversal import name_handles, pretty_thread

    named = {"+16174075277": "Sam Lee", "6174075277": "Sam Lee", "+16149733915": "Ana Ruiz", "6149733915": "Ana Ruiz"}
    assert name_handles("[14:39] +16174075277: home rn", named) == "[14:39] Sam Lee: home rn"
    assert name_handles("[1:00] +19995550000: hi", named) == "[1:00] +19995550000: hi"  # unknown stays
    assert pretty_thread("+16149733915, +16174075277", named) == "Ana, Sam"
    assert pretty_thread("+16149733915, +16174075277, +19995550000, +18885550000", named).endswith("and 1 other")
    assert pretty_thread("Biggers in Paris", named) == "Biggers in Paris"


class _SyncStatusResult:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def all(self):
        return self.rows


class _SyncStatusSession:
    def __init__(self, rows):
        self.rows = rows
        self.statement = None

    def execute(self, statement, _params):
        self.statement = str(statement)
        return _SyncStatusResult(self.rows)


def test_sync_status_reports_age_counts_and_sanitized_cursor():
    from datetime import UTC, datetime

    from igneous.sync_status import sync_status

    session = _SyncStatusSession([{
        "connector": "gmail",
        "account": "me@example.com",
        "cursor": {"history_id": "opaque-history-token", "backfill_page": "opaque-page-token"},
        "updated_at": datetime(2026, 10, 20, 11, 30, tzinfo=UTC),
        "item_count": 42,
    }])

    (status,) = sync_status(session, now=datetime(2026, 10, 20, 12, 0, tzinfo=UTC))

    assert status == {
        "connector": "gmail",
        "account": "me@example.com",
        "status": "known",
        "last_synced_at": datetime(2026, 10, 20, 11, 30, tzinfo=UTC),
        "cursor_age_seconds": 1800,
        "item_count": 42,
        "item_count_scope": "connector",
        "cursor": {"present": True, "keys": ["backfill_page", "history_id"], "gmail_backfill_in_progress": True},
    }
    assert "opaque-history-token" not in repr(status)
    assert "SELECT source AS connector, count(*)::bigint AS item_count" in session.statement
    assert "item_counts.connector = subjects.connector" in session.statement
    assert "raw_meta ->> 'account'" not in session.statement


def test_sync_status_marks_requested_missing_checkpoint_never_synced():
    from igneous.sync_status import sync_status

    session = _SyncStatusSession([{
        "connector": "gmail",
        "account": "me@example.com",
        "cursor": None,
        "updated_at": None,
        "item_count": 42,
    }])

    assert sync_status(session, connector="gmail", account="me@example.com") == [{
        "connector": "gmail",
        "account": "me@example.com",
        "status": "never_synced",
        "last_synced_at": None,
        "cursor_age_seconds": None,
        "item_count": 42,
        "item_count_scope": "connector",
        "cursor": {"present": False, "keys": [], "gmail_backfill_in_progress": False},
    }]


def test_get_sync_is_read_only_status_report():
    from fastapi.testclient import TestClient

    from igneous.db import get_session
    from igneous.main import app

    session = _SyncStatusSession([{
        "connector": "imessage",
        "account": "default",
        "cursor": None,
        "updated_at": None,
        "item_count": 0,
    }])
    app.dependency_overrides[get_session] = lambda: session
    try:
        response = TestClient(app).get("/sync?connector=imessage&account=default")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "syncs": [{
            "connector": "imessage",
            "account": "default",
            "status": "never_synced",
            "last_synced_at": None,
            "cursor_age_seconds": None,
            "item_count": 0,
            "item_count_scope": "connector",
            "cursor": {"present": False, "keys": [], "gmail_backfill_in_progress": False},
        }]
    }
    assert TestClient(app).get("/sync?connector=unknown").status_code == 422


def test_filesystem_connector_walks_sorted_parseable_files_and_emits_hierarchy(tmp_path):
    from igneous.connectors.base import CompletedScan, SourceItem
    from igneous.connectors.filesystem import FilesystemConnector, _root_key

    (tmp_path / "z-last.txt").write_text("last")
    (tmp_path / "projects" / "igneous").mkdir(parents=True)
    (tmp_path / "projects" / "igneous" / "plan.md").write_text("# Plan\nship")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "ignored.txt").write_text("ignore")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("ignore")
    (tmp_path / "photo.jpg").write_bytes(b"not an image parser input")

    emitted = list(FilesystemConnector(tmp_path).sync({}))
    items = [event for event in emitted if isinstance(event, SourceItem)]
    root_key = _root_key(tmp_path)
    assert [item.external_id for item in items] == [
        f"{root_key}/projects/igneous/plan.md", f"{root_key}/z-last.txt"]
    plan = items[0]
    assert plan.body_text == "# Plan\nship"
    assert plan.mime == "text/markdown"
    assert plan.raw_meta == {
        "path": f"{root_key}/projects/igneous/plan.md",
        "folders": [root_key, f"{root_key}/projects", f"{root_key}/projects/igneous"],
        "parser": "text",
        "boundaries": [],
    }
    assert isinstance(emitted[-1], CompletedScan)


def test_filesystem_connector_hashes_file_bytes_and_namespaces_multiple_roots(tmp_path):
    from igneous.connectors.base import SourceItem
    from igneous.connectors.filesystem import FilesystemConnector, _root_key

    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "same.txt").write_text("one")
    (second / "same.txt").write_text("two")
    connector = FilesystemConnector([second, first])
    initial = [event for event in connector.sync({}) if isinstance(event, SourceItem)]
    assert [item.external_id for item in initial] == [
        f"{_root_key(first)}/same.txt", f"{_root_key(second)}/same.txt"]
    (first / "same.txt").write_text("changed")
    changed = [event for event in connector.sync({}) if isinstance(event, SourceItem)]
    assert changed[0].content_hash != initial[0].content_hash


def test_run_sync_prunes_only_after_completed_scan(monkeypatch):
    from igneous.connectors import base

    class Result:
        def __init__(self, deleted=False):
            self.rowcount = 1 if deleted else 0

        def first(self):
            return None

    class Session:
        def __init__(self):
            self.statements = []
            self.commits = 0

        def execute(self, statement, values=None):
            self.statements.append(str(statement))
            return Result("DELETE FROM source_items" in str(statement))

        def commit(self):
            self.commits += 1

    class Complete:
        name = "fs"

        def sync(self, cursor):
            yield base.SourceItem("fs", "kept.txt", "kept.txt", "body")
            yield base.CompletedScan({}, "fs", frozenset({"kept.txt"}))

    session = Session()
    monkeypatch.setattr(base, "load_cursor", lambda *args: {})
    monkeypatch.setattr(base, "save_cursor", lambda *args: None)
    assert base.run_sync(session, Complete())["deleted"] == 1
    assert any("DELETE FROM source_items" in statement for statement in session.statements)

    class Interrupted:
        name = "fs"

        def sync(self, cursor):
            yield base.SourceItem("fs", "kept.txt", "kept.txt", "body")
            yield base.Checkpoint({})
            raise RuntimeError("interrupted")

    session = Session()
    with pytest.raises(RuntimeError, match="interrupted"):
        base.run_sync(session, Interrupted())
    assert not any("DELETE FROM source_items" in statement for statement in session.statements)


def test_filesystem_connector_rejects_missing_roots(tmp_path):
    from igneous.connectors.filesystem import FilesystemConnector

    with pytest.raises(ValueError, match="must be directories"):
        FilesystemConnector(tmp_path / "does-not-exist")
