"""Document text extraction (parsers.py): formats, page and section boundaries, and what is never parsed."""
import subprocess
import sys
from io import BytesIO

from igneous.parsers import parse_document, supported


def test_parse_plain_text_markdown_and_binary_are_nonfatal():
    markdown = parse_document(b"# Title\r\n\r\nBody", "notes.md")
    assert (markdown.status, markdown.mime, markdown.parser, markdown.text) == (
        "ok", "text/markdown", "text", "# Title\n\nBody")
    unsupported = parse_document(b"\x89PNG\r\n\x1a\n", "image.png")
    assert unsupported.status == "unsupported" and unsupported.text == "" and unsupported.error
    binary = parse_document(b"\x00\x01", "bad.txt")
    assert binary.status == "binary" and binary.text == "" and binary.error


def test_parse_pdf_with_page_boundaries():
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
    from docx import Document
    from openpyxl import Workbook
    from pptx import Presentation

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
    parsed = parse_document(b"not a document", "broken.docx")
    assert parsed.status == "corrupt" and parsed.parser == "docx" and parsed.text == "" and parsed.error


def test_legacy_office_files_are_not_read_as_ooxml():
    """A .doc/.xls/.ppt (a Drive file with only its MIME type) is not OOXML: unsupported, not "corrupt"."""
    for name, mime in (("memo", "application/msword"), ("budget", "application/vnd.ms-excel"),
                       ("deck", "application/vnd.ms-powerpoint")):
        parsed = parse_document(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1 legacy compound file", name, mime)
        assert parsed.status in ("unsupported", "binary") and parsed.parser is None, (name, parsed)
        assert not supported(name, mime)


def test_support_is_known_before_reading():
    assert supported("plan.docx") and supported("notes.MD") and supported("x", "text/plain")
    assert supported("export", "application/pdf")
    assert not supported("movie.mp4", "video/mp4") and not supported("archive.zip") and not supported(None)


def test_the_document_libraries_load_only_when_needed():
    code = ("import sys, igneous.parsers; print(any(m in sys.modules for m in ('docx', 'openpyxl', 'pptx', 'pypdf')))")
    assert subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.strip() == "False"
