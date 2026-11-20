"""Text extraction for uploaded documents.

Images go to the OCR service. A PDF must not: the service reads images, so a
PDF posted to it comes back empty and the upload used to succeed with
`extracted_text: null` - after which "summarise this report" had nothing to
summarise. A PDF is read here instead:

1. its own text layer (pypdf, pure Python) - exact, free, and what most
   lab reports and discharge summaries are;
2. if that is empty (a scan), each page is rendered and OCRed, when PyMuPDF is
   installed. Without it a scanned PDF is reported as unreadable, honestly.

Every outcome carries a reason, so the app can say *why* nothing came back.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger(__name__)

MAX_PAGES = 20
MAX_SCANNED_PAGES = 5  # rendering and OCR are the expensive part
MAX_CHARS = 12000
_MIN_USEFUL_CHARS = 20  # fewer than this from a PDF means "no text layer"


@dataclass
class Extraction:
    text: str = ""
    method: Optional[str] = None  # "pdf_text" | "pdf_ocr" | "image_ocr"
    error: Optional[str] = None  # why there is no text; None when there is

    @property
    def ok(self) -> bool:
        return bool(self.text.strip())

    @property
    def status(self) -> str:
        return "ok" if self.ok else "failed" if self.error else "empty"


def is_pdf(raw: bytes) -> bool:
    return raw[:5] == b"%PDF-"


def _pdf_text_layer(raw: bytes) -> tuple[str, Optional[str], int]:
    """(text, error, page_count) from the PDF's own text."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return "", "pdf reading is not installed on the server (pypdf)", 0

    try:
        reader = PdfReader(io.BytesIO(raw))
        if reader.is_encrypted:
            try:
                if not reader.decrypt(""):
                    return "", "the PDF is password protected", 0
            except Exception:
                return "", "the PDF is password protected", 0
        pages = len(reader.pages)
        parts: list[str] = []
        total = 0
        for page in list(reader.pages)[:MAX_PAGES]:
            chunk = (page.extract_text() or "").strip()
            if chunk:
                parts.append(chunk)
                total += len(chunk)
            if total >= MAX_CHARS:
                break
        return "\n\n".join(parts)[:MAX_CHARS], None, pages
    except Exception as exc:  # corrupt or unusual PDFs raise all sorts
        logger.warning("pdf text extraction failed: %s", exc)
        return "", "the PDF could not be read (it may be damaged)", 0


def _pdf_scanned(raw: bytes, ocr: Callable[[bytes], str]) -> tuple[str, Optional[str]]:
    """Render the first pages and OCR them. Needs PyMuPDF."""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return "", "this looks like a scanned PDF and scanned-PDF reading is not installed"

    parts: list[str] = []
    try:
        with fitz.open(stream=raw, filetype="pdf") as doc:
            for index in range(min(doc.page_count, MAX_SCANNED_PAGES)):
                pixmap = doc[index].get_pixmap(matrix=fitz.Matrix(2, 2))
                page_text = (ocr(pixmap.tobytes("png")) or "").strip()
                if page_text:
                    parts.append(page_text)
    except Exception as exc:
        logger.warning("scanned pdf extraction failed: %s", exc)
        return "", "the scanned PDF could not be read"
    return "\n\n".join(parts)[:MAX_CHARS], None


def extract_document(raw: bytes, session) -> Extraction:
    """Text of an uploaded file, with the reason when there is none."""
    if not raw:
        return Extraction(error="the file is empty")

    from packages.product.ocr import OcrService

    service = OcrService(session)

    def ocr_text(image: bytes) -> str:
        result = service.read(image)
        return result.text if result and result.ok else ""

    if is_pdf(raw):
        text, error, _pages = _pdf_text_layer(raw)
        if len(text.strip()) >= _MIN_USEFUL_CHARS:
            return Extraction(text=text, method="pdf_text")
        if error:
            return Extraction(error=error)
        scanned, scan_error = _pdf_scanned(raw, ocr_text)
        if scanned.strip():
            return Extraction(text=scanned, method="pdf_ocr")
        return Extraction(error=scan_error or "no readable text was found in the PDF")

    result = service.read(raw)
    if result and result.ok and result.text.strip():
        return Extraction(text=result.text, method="image_ocr")
    reason = getattr(result, "error", None) if result else None
    return Extraction(error=reason or "no readable text was found in the file")
