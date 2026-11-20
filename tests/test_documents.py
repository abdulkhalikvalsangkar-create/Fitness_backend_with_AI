"""Uploaded PDFs are read from their own text; failures say why."""

import unittest
from unittest import mock

from fastapi import HTTPException

from apps.api import actions
from packages.product import documents


def make_pdf(*lines: str) -> bytes:
    from fpdf import FPDF

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=12)
    for line in lines:
        pdf.cell(0, 8, line, new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())


def blank_pdf() -> bytes:
    from fpdf import FPDF

    pdf = FPDF()
    pdf.add_page()
    return bytes(pdf.output())


class PdfExtractionTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch("packages.product.ocr.OcrService")
        self.ocr = patcher.start()
        self.addCleanup(patcher.stop)

    def test_text_pdf_is_read_without_the_image_ocr_service(self) -> None:
        raw = make_pdf("Lab report", "Hemoglobin 13.5 g/dL", "Glucose fasting 92 mg/dL")
        result = documents.extract_document(raw, mock.Mock())
        self.assertTrue(result.ok)
        self.assertEqual((result.method, result.status), ("pdf_text", "ok"))
        self.assertIn("Hemoglobin 13.5", result.text)
        self.ocr.return_value.read.assert_not_called()

    def test_scanned_pdf_falls_back_to_page_ocr(self) -> None:
        with mock.patch.object(documents, "_pdf_scanned", return_value=("Scanned: TSH 2.1", None)):
            result = documents.extract_document(blank_pdf(), mock.Mock())
        self.assertEqual((result.method, result.text), ("pdf_ocr", "Scanned: TSH 2.1"))

    def test_unreadable_scan_reports_a_reason(self) -> None:
        reason = "scanned-PDF reading is not installed"
        with mock.patch.object(documents, "_pdf_scanned", return_value=("", reason)):
            result = documents.extract_document(blank_pdf(), mock.Mock())
        self.assertFalse(result.ok)
        self.assertEqual((result.status, result.error), ("failed", reason))

    def test_damaged_pdf_reports_a_reason_instead_of_raising(self) -> None:
        result = documents.extract_document(b"%PDF-1.4 this is not really a pdf", mock.Mock())
        self.assertFalse(result.ok)
        self.assertTrue(result.error)

    def test_empty_file(self) -> None:
        self.assertEqual(documents.extract_document(b"", mock.Mock()).error, "the file is empty")

    def test_images_still_use_ocr(self) -> None:
        self.ocr.return_value.read.return_value = mock.Mock(ok=True, text="INGREDIENTS: sugar")
        result = documents.extract_document(b"\x89PNG....", mock.Mock())
        self.assertEqual((result.method, result.text), ("image_ocr", "INGREDIENTS: sugar"))

    def test_image_with_no_text_says_so(self) -> None:
        self.ocr.return_value.read.return_value = mock.Mock(ok=False, text="", error="OCR service returned 500")
        result = documents.extract_document(b"\x89PNG....", mock.Mock())
        self.assertEqual((result.status, result.error), ("failed", "OCR service returned 500"))


class ChatReadsEarlierUploadsTests(unittest.TestCase):
    def principal(self):
        return mock.Mock(user_id="u1")

    def blob(self, raw: bytes, mime: str):
        blob = mock.Mock(mime_type=mime)
        blob.read.return_value = raw
        return blob

    def test_a_pdf_uploaded_without_text_is_read_when_chat_asks_about_it(self) -> None:
        store = mock.Mock()
        store.get.return_value = self.blob(make_pdf("Cholesterol 180 mg/dL total"), "application/pdf")
        text, problem = actions._read_document_now(store, "b1", self.principal(), mock.Mock())
        self.assertIn("Cholesterol 180", text)
        self.assertIsNone(problem)
        store.set_extracted_text.assert_called_once()

    def test_an_unreadable_pdf_is_reported_not_answered_with_small_talk(self) -> None:
        store = mock.Mock()
        store.get.return_value = self.blob(b"%PDF-1.4 broken", "application/pdf")
        text, problem = actions._read_document_now(store, "b1", self.principal(), mock.Mock())
        self.assertIsNone(text)
        self.assertIn("couldn't read any text from this PDF", problem)

    def test_an_image_without_text_is_left_alone(self) -> None:
        store = mock.Mock()
        store.get.return_value = self.blob(b"\x89PNG....", "image/png")
        with mock.patch("packages.product.ocr.OcrService") as ocr:
            ocr.return_value.read.return_value = mock.Mock(ok=False, text="", error="x")
            self.assertEqual(actions._read_document_now(store, "b1", self.principal(), mock.Mock()), (None, None))

    def test_missing_blob(self) -> None:
        store = mock.Mock()
        store.get.return_value = None
        self.assertEqual(actions._read_document_now(store, "nope", self.principal(), mock.Mock()), (None, None))


class DocumentRoutingTests(unittest.TestCase):
    def test_a_question_about_an_uploaded_document_is_not_small_talk(self) -> None:
        from packages.domain.enums import RouteLabel
        from packages.orchestrator.router import Router

        router = Router.__new__(Router)
        for question in ("summarise this report", "ok", "thanks"):
            message = f"user uploaded document text: hemoglobin 13.5 question: {question}"
            decision = router._stage_rules(message, has_attachments=False)
            self.assertEqual(decision.label, RouteLabel.PERSONAL)


if __name__ == "__main__":
    unittest.main()
