"""Transport-agnostic, non-throwing document text extraction boundary."""
from __future__ import annotations

import io
import mimetypes
import re
from dataclasses import dataclass
from pathlib import PurePath
from typing import Literal

from docx import Document
from openpyxl import load_workbook
from pptx import Presentation
from pypdf import PdfReader

ParseStatus = Literal["ok", "unsupported", "binary", "corrupt"]


@dataclass(frozen=True)
class ParsedDocument:
    """Result of parsing upload or connector bytes.

    ``boundaries`` deliberately contains only JSON primitives. Offsets are zero-based,
    half-open indexes into ``text``, allowing a later chunker to respect pages, headings,
    slides, and sheets without needing format-specific dependencies.
    """

    text: str
    mime: str | None
    parser: str | None
    boundaries: list[dict[str, int | str]]
    status: ParseStatus
    error: str | None = None


_FORMATS = {
    ".pdf": ("application/pdf", "pdf"),
    ".docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx"),
    ".xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx"),
    ".pptx": ("application/vnd.openxmlformats-officedocument.presentationml.presentation", "pptx"),
    ".md": ("text/markdown", "text"),
    ".markdown": ("text/markdown", "text"),
    ".txt": ("text/plain", "text"),
    ".rst": ("text/x-rst", "text"),
    ".log": ("text/plain", "text"),
    ".csv": ("text/csv", "text"),
    ".json": ("application/json", "text"),
    ".yaml": ("application/yaml", "text"),
    ".yml": ("application/yaml", "text"),
    ".xml": ("application/xml", "text"),
    ".html": ("text/html", "text"),
    ".htm": ("text/html", "text"),
    ".py": ("text/x-python", "text"),
    ".js": ("text/javascript", "text"),
    ".ts": ("text/typescript", "text"),
    ".tsx": ("text/tsx", "text"),
    ".jsx": ("text/jsx", "text"),
    ".java": ("text/x-java", "text"),
    ".c": ("text/x-c", "text"),
    ".h": ("text/x-c", "text"),
    ".cpp": ("text/x-c++", "text"),
    ".cc": ("text/x-c++", "text"),
    ".cs": ("text/x-csharp", "text"),
    ".go": ("text/x-go", "text"),
    ".rs": ("text/x-rust", "text"),
    ".rb": ("text/x-ruby", "text"),
    ".php": ("text/x-php", "text"),
    ".sh": ("text/x-shellscript", "text"),
    ".sql": ("application/sql", "text"),
    ".css": ("text/css", "text"),
    ".toml": ("application/toml", "text"),
    ".ini": ("text/plain", "text"),
}
_MIME_FORMATS = {mime: parser for mime, parser in _FORMATS.values()}
_MIME_FORMATS.update({
    "application/vnd.ms-excel": "xlsx",
    "application/vnd.ms-powerpoint": "pptx",
    "application/msword": "docx",
})
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$", re.MULTILINE)


def parse_document(data: bytes, filename: str | None = None, mime: str | None = None) -> ParsedDocument:
    """Extract text from bytes without depending on the upload transport.

    Unknown, binary, and malformed inputs are returned as typed nonfatal outcomes rather
    than raising, so connectors can record or skip an attachment independently.
    """
    resolved_mime, parser = _detect(data, filename, mime)
    if parser is None:
        status: ParseStatus = "unsupported" if resolved_mime else "binary" if _looks_binary(data) else "unsupported"
        message = "input contains binary data" if status == "binary" else "unsupported document type"
        return ParsedDocument("", resolved_mime, None, [], status, message)
    if parser == "text":
        return _parse_text(data, resolved_mime)
    try:
        if parser == "pdf":
            text, boundaries = _parse_pdf(data)
        elif parser == "docx":
            text, boundaries = _parse_docx(data)
        elif parser == "xlsx":
            text, boundaries = _parse_xlsx(data)
        else:
            text, boundaries = _parse_pptx(data)
    except Exception as exc:  # parsers expose several library-specific corrupt-file errors
        return ParsedDocument("", resolved_mime, parser, [], "corrupt", f"could not parse document: {exc}")
    return ParsedDocument(_normalize(text), resolved_mime, parser, boundaries, "ok")


def _detect(data: bytes, filename: str | None, mime: str | None) -> tuple[str | None, str | None]:
    supplied_mime = mime.lower().split(";", 1)[0].strip() if mime else None
    extension = PurePath(filename).suffix.lower() if filename else ""
    if data.startswith(b"%PDF-"):
        return "application/pdf", "pdf"
    if extension in _FORMATS:
        return _FORMATS[extension]
    if supplied_mime in _MIME_FORMATS:
        return supplied_mime, _MIME_FORMATS[supplied_mime]
    if supplied_mime and supplied_mime.startswith("text/"):
        return supplied_mime, "text"
    guessed_mime = mimetypes.guess_type(filename or "")[0]
    return supplied_mime or guessed_mime, None


def _parse_text(data: bytes, mime: str | None) -> ParsedDocument:
    if _looks_binary(data):
        return ParsedDocument("", mime, "text", [], "binary", "input contains binary data")
    try:
        return ParsedDocument(_normalize(data.decode("utf-8-sig")), mime, "text", [], "ok")
    except UnicodeDecodeError:
        # UTF-16 is common enough to be a useful text boundary; arbitrary legacy encodings
        # are intentionally not guessed, since they can turn binary attachments into garbage.
        if data.startswith((b"\xff\xfe", b"\xfe\xff")):
            try:
                return ParsedDocument(_normalize(data.decode("utf-16")), mime, "text", [], "ok")
            except UnicodeDecodeError:
                pass
        return ParsedDocument("", mime, "text", [], "binary", "input is not UTF-8 text")


def _parse_pdf(data: bytes) -> tuple[str, list[dict[str, int | str]]]:
    parts, boundaries, offset = [], [], 0
    for index, page in enumerate(PdfReader(io.BytesIO(data)).pages, start=1):
        content = _normalize(page.extract_text() or "")
        if parts:
            offset += 2
        parts.append(content)
        boundaries.append({"kind": "page", "index": index, "start": offset, "end": offset + len(content)})
        offset += len(content)
    return "\n\n".join(parts), boundaries


def _parse_docx(data: bytes) -> tuple[str, list[dict[str, int | str]]]:
    document, parts, boundaries, offset = Document(io.BytesIO(data)), [], [], 0
    for paragraph in document.paragraphs:
        content = _normalize(paragraph.text)
        if not content:
            continue
        if parts:
            offset += 1
        parts.append(content)
        style = paragraph.style.name if paragraph.style else ""
        match = re.match(r"Heading\s+([1-9][0-9]*)$", style, re.IGNORECASE)
        if match:
            boundaries.append({"kind": "heading", "level": int(match.group(1)), "title": content,
                               "start": offset, "end": offset + len(content)})
        offset += len(content)
    for table_index, table in enumerate(document.tables, start=1):
        content = "\n".join("\t".join(_normalize(str(cell.text)) for cell in row.cells) for row in table.rows)
        if not content:
            continue
        if parts:
            offset += 1
        parts.append(content)
        boundaries.append({"kind": "table", "index": table_index, "start": offset, "end": offset + len(content)})
        offset += len(content)
    return "\n".join(parts), boundaries


def _parse_xlsx(data: bytes) -> tuple[str, list[dict[str, int | str]]]:
    workbook, parts, boundaries, offset = load_workbook(io.BytesIO(data), read_only=True, data_only=True), [], [], 0
    try:
        for index, sheet in enumerate(workbook.worksheets, start=1):
            rows = []
            for row in sheet.iter_rows(values_only=True):
                if any(value is not None for value in row):
                    rows.append("\t".join("" if value is None else _normalize(str(value)) for value in row).rstrip("\t"))
            content = "\n".join(rows)
            if parts:
                offset += 2
            parts.append(content)
            boundaries.append({"kind": "sheet", "index": index, "name": sheet.title,
                               "start": offset, "end": offset + len(content)})
            offset += len(content)
    finally:
        workbook.close()
    return "\n\n".join(parts), boundaries


def _parse_pptx(data: bytes) -> tuple[str, list[dict[str, int | str]]]:
    presentation, parts, boundaries, offset = Presentation(io.BytesIO(data)), [], [], 0
    for index, slide in enumerate(presentation.slides, start=1):
        content = "\n".join(_normalize(shape.text) for shape in slide.shapes if hasattr(shape, "text") and shape.text)
        if parts:
            offset += 2
        parts.append(content)
        boundaries.append({"kind": "slide", "index": index, "start": offset, "end": offset + len(content)})
        offset += len(content)
    return "\n\n".join(parts), boundaries


def _normalize(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _looks_binary(data: bytes) -> bool:
    if b"\x00" in data and not data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return True
    if not data:
        return False
    controls = sum(byte < 32 and byte not in (9, 10, 12, 13) for byte in data)
    return controls / len(data) > 0.10
