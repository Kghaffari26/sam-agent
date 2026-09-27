"""SAM.gov notice attachments for bid research: naming, download and text
extraction (PDF and DOCX).

A notice's attachments are its `resourceLinks` (download URLs on sam.gov, no API
key needed). The search response doesn't carry file names, so the research tools
name them `attachment_1`, `attachment_2`, ... in `resourceLinks` order. Each
download is one request against the day's SAM budget (`fetch_sam.SamBudget`),
made through agents-core's conditional-GET `Http.download`, and kept under
`.cache/grants/attachments/` with its extracted text next to it, so an attachment
already read is served again without a request.
"""

from __future__ import annotations

import io
import logging
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from agents.grants.models import Opportunity

log = logging.getLogger(__name__)

MAX_PDF_PAGES = 40
_RESOURCE_RE = re.compile(r"/files/([A-Za-z0-9_-]+)/download")
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_WS_RE = re.compile(r"[ \t\r\f\v]+")


class UnsupportedAttachment(ValueError):
    """Not a PDF or DOCX (or not readable as one)."""


def attachment_cache_dir() -> Path:
    return Path(".cache") / "grants" / "attachments"


def resource_id(url: str) -> str:
    m = _RESOURCE_RE.search(url)
    if m:
        return m.group(1)
    return re.sub(r"[^A-Za-z0-9_-]", "_", url.rstrip("/").rsplit("/", 1)[-1])[:64] or "file"


def attachment_names(opp: Opportunity) -> list[tuple[str, str]]:
    """(name, url) for each attachment, in `resourceLinks` order."""
    return [(f"attachment_{i}", url) for i, url in enumerate(opp.attachments, start=1)]


def _tidy(text: str) -> str:
    lines = [_WS_RE.sub(" ", line).strip() for line in text.splitlines()]
    out: list[str] = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    return "\n".join(out).strip()


def pdf_text(data: bytes) -> str:
    from pypdf import PdfReader  # imported lazily: only research needs it
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [p.extract_text() or "" for p in reader.pages[:MAX_PDF_PAGES]]
    except (PdfReadError, ValueError, KeyError, TypeError) as e:
        raise UnsupportedAttachment(f"unreadable PDF: {e}") from e
    return _tidy("\n".join(pages))


def docx_text(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            xml = z.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError) as e:
        raise UnsupportedAttachment(f"unreadable DOCX: {e}") from e
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as e:
        raise UnsupportedAttachment(f"unreadable DOCX: {e}") from e
    paragraphs = []
    for p in root.iter(f"{_W}p"):
        parts = []
        for node in p.iter():
            if node.tag == f"{_W}t" and node.text:
                parts.append(node.text)
            elif node.tag == f"{_W}tab":
                parts.append("\t")
        paragraphs.append("".join(parts))
    return _tidy("\n".join(paragraphs))


def extract_text(data: bytes) -> tuple[str, str]:
    """(kind, text) for a PDF or DOCX file; raises `UnsupportedAttachment` otherwise."""
    if data[:5] == b"%PDF-":
        return "pdf", pdf_text(data)
    if data[:2] == b"PK":
        return "docx", docx_text(data)
    raise UnsupportedAttachment("only PDF and DOCX attachments can be read")
