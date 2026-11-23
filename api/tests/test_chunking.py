"""Chunking and embedding: documents along the pages, sections, slides and sheets the parser recorded, every chunk
under its record's title, every source embedded locally, and each vector tied to the model that made it."""
from io import BytesIO
from types import SimpleNamespace

from igneous.rag import chunking

SECRET = "sk-ant-" + "a" * 40


def item(body, boundaries, source="drive", title="plan.pdf", **meta):
    return SimpleNamespace(source=source, title=title, body_text=body, occurred_at=None,
                           raw_meta={"boundaries": boundaries, **meta})


def pages(*texts):
    """Body and page boundaries exactly as parsers._parse_pdf builds them."""
    bounds, offset = [], 0
    for i, t in enumerate(texts, start=1):
        if i > 1:
            offset += 2
        bounds.append({"kind": "page", "index": i, "start": offset, "end": offset + len(t)})
        offset += len(t)
    return "\n\n".join(texts), bounds


def test_small_pages_pack_whole_and_each_chunk_says_where_it_is():
    body, bounds = pages("Budget overview", "Hiring plan", "Risks")
    assert chunking.chunks_for(item(body, bounds)) == [
        "plan.pdf, page 1 to page 3\nBudget overview\n\nHiring plan\n\nRisks"]


def test_a_page_never_shares_a_chunk_unless_packed_whole():
    long_page = "word " * 600   # 3000 characters: split on its own, with overlap
    body, bounds = pages("Intro", long_page.strip(), "Appendix")
    chunks = chunking.chunks_for(item(body, bounds))
    assert chunks[0] == "plan.pdf, page 1\nIntro"
    assert all(c.startswith("plan.pdf, page 2\n") for c in chunks[1:-1]) and len(chunks) >= 4
    assert chunks[-1] == "plan.pdf, page 3\nAppendix"


def test_docx_sections_run_from_heading_to_heading_and_tables_follow():
    from docx import Document

    from igneous.parsers import parse_document

    doc = Document()
    doc.add_paragraph("Draft for the team")
    doc.add_heading("Background", level=1)
    doc.add_paragraph("Why we started")
    doc.add_heading("Plan", level=1)
    doc.add_paragraph("Ship in May")
    table = doc.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text, table.rows[0].cells[1].text = "owner", "Sam"
    data = BytesIO()
    doc.save(data)
    parsed = parse_document(data.getvalue(), "proposal.docx")
    units = chunking.sections(item(parsed.text, parsed.boundaries, title="proposal.docx"))
    assert units == [("", "Draft for the team"), ("Background", "Background\nWhy we started"),
                     ("Plan", "Plan\nShip in May"), ("table 1", "owner\tSam")]


def test_sheets_are_named():
    body = "a\t1\n\nb\t2"
    bounds = [{"kind": "sheet", "index": 1, "name": "Budget", "start": 0, "end": 3},
              {"kind": "sheet", "index": 2, "name": "Staff", "start": 5, "end": 8}]
    assert chunking.sections(item(body, bounds, title="q3.xlsx")) == [("sheet Budget", "a\t1"), ("sheet Staff", "b\t2")]


def test_untrusted_boundaries_fall_back_to_plain_splitting():
    body, bounds = pages("one", "two")
    # A redacted local file whose recorded offsets point into the unredacted text.
    unredacted_offsets = item(body, bounds, source="fs", redactions={"anthropic_key": 1})
    assert chunking.chunks_for(unredacted_offsets) == [f"plan.pdf\n{body}"]
    assert chunking.chunks_for(item(body, [{"kind": "page", "index": 1, "start": 0, "end": 999}])) == [f"plan.pdf\n{body}"]
    assert chunking.chunks_for(item(body, [])) == [f"plan.pdf\n{body}"]


def test_every_kind_of_record_puts_its_title_in_every_chunk():
    long = "word " * 900  # three chunks
    for source, title in (("fs", "notes.md"), ("ai_chats", "Claude Code: igneous, 2025-03-01"),
                          ("github", "Fix the build"), ("imessage", "Chat with Sam Lee"), ("linkedin", "Profile"),
                          ("drive", "Budget 2025"), ("takeout_chrome", "Chrome history, 2025-01-02")):
        chunks = chunking.chunks_for(item(long.strip(), [], source=source, title=title))
        assert len(chunks) == 3 and all(c.startswith(f"{title}\n") for c in chunks), source
    # A note whose first line is its title doesn't repeat it; untitled records have nothing to add.
    assert chunking.chunks_for(item("Groceries\n- eggs", [], source="notes", title="Groceries")) == ["Groceries\n- eggs"]
    assert chunking.chunks_for(item("just text", [], source="fs", title=None)) == ["just text"]
    # Mail and calendar keep their own headers.
    assert chunking.chunks_for(item("Hello", [], source="gmail", title="Lunch")) == ["Subject: Lunch\nHello"]


def test_redaction_moves_a_local_files_page_boundaries(tmp_path):
    """The fs connector redacts after parsing: every replacement shifts what follows it."""
    from pptx import Presentation

    from igneous.connectors.base import SourceItem
    from igneous.connectors.filesystem import FilesystemConnector

    deck = Presentation()
    for words in (f"Setup uses {SECRET} for now", "Second slide", "Third slide"):
        deck.slides.add_slide(deck.slide_layouts[5]).shapes.add_textbox(0, 0, 1, 1).text = words
    deck.save(tmp_path / "deck.pptx")
    (doc,) = [x for x in FilesystemConnector(tmp_path).sync({}) if isinstance(x, SourceItem)]
    assert SECRET not in doc.body_text and doc.raw_meta["redactions"] == {"anthropic_key": 1}
    assert [doc.body_text[b["start"]:b["end"]] for b in doc.raw_meta["boundaries"]] == [
        "Setup uses [REDACTED:anthropic_key] for now", "Second slide", "Third slide"]
    assert chunking.chunks_for(doc) == [
        "deck.pptx, slide 1 to slide 3\nSetup uses [REDACTED:anthropic_key] for now\n\nSecond slide\n\nThird slide"]


def _record(s, source, ext, title, body, raw=None, chunk=None) -> tuple[int, int]:
    """A source item with its node, and optionally one chunk made under no layout (an older one), with a vector."""
    import json

    from sqlalchemy import text

    sid = s.execute(text("""
        INSERT INTO source_items (source, external_id, content_hash, title, body_text, raw_meta)
        VALUES (:s, :e, :e, :t, :b, CAST(:m AS jsonb)) RETURNING id"""),
        {"s": source, "e": ext, "t": title, "b": body, "m": json.dumps(raw or {})}).scalar_one()
    nid = s.execute(text("INSERT INTO nodes (type, key, name, source_item_id) VALUES (:t, :k, :k, :i) RETURNING id"),
                    {"t": "Message" if source in ("gmail", "email") else "File", "k": ext, "i": sid}).scalar_one()
    if chunk is not None:
        s.execute(text("""INSERT INTO chunks (source_item_id, node_id, ord, text, meta, embedding, embedding_model)
                          VALUES (:i, :n, 0, :c, '{}', CAST(:v AS vector), :m)"""),
                  {"i": sid, "n": nid, "c": chunk, "v": str([0.5] * 384), "m": "BAAI/bge-small-en-v1.5"})
    return sid, nid


def test_items_chunked_under_an_older_layout_are_chunked_again_once_and_keep_unchanged_vectors(db):
    from datetime import UTC, datetime

    from sqlalchemy import text

    from igneous.pipeline import advance, watermark

    body, bounds = pages("First page", "Second page")
    doc, _ = _record(db, "drive", "doc", "plan.pdf", body, {"boundaries": bounds}, chunk=body)
    note, _ = _record(db, "fs", "notes.md", "notes.md", "Remember the milk", chunk="Remember the milk")
    mail, _ = _record(db, "gmail", "mail", "Hi", "hello", chunk="Subject: Hi\nhello")
    before = db.execute(text("SELECT id FROM chunks WHERE source_item_id = :i"), {"i": mail}).scalar_one()
    watermark(db, chunking.STAGE)
    advance(db, chunking.STAGE, datetime.now(UTC))  # all three are behind the watermark
    assert chunking.run(db) == 3
    rows = {r.source_item_id: r for r in db.execute(text(
        "SELECT id, source_item_id, text, meta->>'layout' AS layout, embedding IS NOT NULL AS has_vector FROM chunks"))}
    assert (rows[doc].text, rows[doc].has_vector) == ("plan.pdf, page 1 to page 2\nFirst page\n\nSecond page", False)
    assert (rows[note].text, rows[note].has_vector) == ("notes.md\nRemember the milk", False)
    # The email's chunk comes out the same: kept, vector and all, under the new layout.
    assert (rows[mail].id, rows[mail].text, rows[mail].has_vector) == (before, "Subject: Hi\nhello", True)
    assert {r.layout for r in rows.values()} == {str(chunking.LAYOUT)}
    assert chunking.run(db) == 0  # once


def test_every_source_is_embedded_locally_and_each_vector_names_its_model(db, monkeypatch):
    from sqlalchemy import text

    from igneous.rag import embed

    monkeypatch.setattr(embed.settings, "local_sources_model_enabled", False)  # hosted models only
    calls: list[list[str]] = []
    monkeypatch.setattr(embed, "embed_texts", lambda texts: calls.append(texts) or [[0.1] * embed.DIM for _ in texts])
    for source, ext in (("fs", "a.md"), ("notes", "n1"), ("gmail", "m1")):
        sid, nid = _record(db, source, ext, ext, "text")
        db.execute(text("INSERT INTO chunks (source_item_id, node_id, ord, text) VALUES (:i, :n, 0, :t)"),
                   {"i": sid, "n": nid, "t": f"{ext} text"})
    assert embed.run(db) == 3
    assert db.execute(text("SELECT DISTINCT embedding_model FROM chunks")).scalars().all() == [
        embed.settings.embedding_model]
    # Vectors from another model are made again; current ones are left alone.
    db.execute(text("UPDATE chunks SET embedding_model = 'another/model' WHERE text = 'a.md text'"))
    calls.clear()
    reported: list[tuple[int, int]] = []
    assert embed.run(db, progress=lambda done, total: reported.append((done, total))) == 1
    assert calls == [["a.md text"]] and reported == [(1, 1)]
    assert embed.run(db) == 0


def test_a_model_of_another_dimension_is_refused_before_anything_is_written(db, monkeypatch):
    import pytest
    from sqlalchemy import text

    from igneous.rag import embed

    sid, nid = _record(db, "fs", "a.md", "a.md", "text")
    db.execute(text("INSERT INTO chunks (source_item_id, node_id, ord, text) VALUES (:i, :n, 0, 'x')"),
               {"i": sid, "n": nid})
    monkeypatch.setattr(embed, "embed_texts", lambda texts: [[0.1] * 768 for _ in texts])
    with pytest.raises(RuntimeError, match="768-dimensional"):
        embed.run(db)
    assert db.execute(text("SELECT count(embedding) FROM chunks")).scalar() == 0


def test_fastembed_keeps_its_models_in_the_data_folder(monkeypatch, tmp_path):
    from igneous import providers
    from igneous.config import settings

    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "model_cache_dir", "")
    for provider in (providers._embedder, providers._reranker):
        provider.cache_clear()
        assert provider().cache_dir == str(tmp_path / "models")  # built lazily: nothing is downloaded here
        provider.cache_clear()
    monkeypatch.setattr(settings, "model_cache_dir", "/srv/models")
    assert settings.models_dir == "/srv/models"
