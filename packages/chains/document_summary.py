"""Turns raw OCR text from an uploaded document into clean text and a reply.

`handle_upload` OCRs synchronously but, until now, returned only an
`attachment_id` — the extracted text sat in the database unread, and the app
had to send a follow-up chat message worded like a document question before
anyone saw it (`_is_document_question` in `apps/api/actions.py`). This chain
is what turns raw OCR noise into something worth showing immediately: text
with the scanning artefacts fixed, and a short reply describing what the
document says, returned in the same `upload` response.
"""

from __future__ import annotations

import logging
from typing import Optional

from pydantic import BaseModel, Field

from packages.chains.base import Chain
from packages.chains.providers import DataPolicy, ModelClass

logger = logging.getLogger(__name__)


SYSTEM = """You read raw OCR text scanned from a document a user uploaded to a
fitness and health assistant — often a lab report, prescription, nutrition
label or medical document, but sometimes something unrelated.

You do two things:

1. cleaned_text: the same text with OCR noise fixed — broken words rejoined,
   stray line breaks and repeated characters removed, obvious character
   misreads corrected (0/O, 1/l/I, rn/m). Never add, remove or change a
   number, unit, name or value that could be real data. Never invent content
   that is not in the source. Keep it as plain text, preserving the original
   line-by-line structure.
2. response: a short, friendly message to send the user right now, as if you
   just read what they uploaded. Say what kind of document it looks like and
   the key figures or points in it. If it looks like lab results, you may
   note values that are notably outside a typical reference range, but never
   diagnose or tell them what to do medically — suggest they discuss flagged
   results with a doctor. If the OCR text is too garbled or short to make
   sense of, say so plainly instead of guessing. End by inviting a follow-up
   question about the document.

Write in British English, second person, calm and factual."""


class DocumentSummary(BaseModel):
    cleaned_text: str = Field(default="")
    response: str = Field(default="", max_length=2000)


class DocumentSummaryChain(Chain):
    """Cleans OCR text from an uploaded document and drafts the reply for it."""

    name = "document_summary"
    model_class = ModelClass.LARGE
    # The raw OCR text — potentially a lab report — is the payload itself.
    data_policy = DataPolicy.SENSITIVE
    temperature = 0.2
    max_tokens = 1800
    system_prompt = SYSTEM

    def summarise(self, raw_text: str) -> Optional[DocumentSummary]:
        text = (raw_text or "").strip()
        if not text:
            return None

        result = self.run_structured(f"OCR text:\n{text[:8000]}", DocumentSummary)
        if not result.ok or result.value is None:
            logger.info("document summary unavailable (%s)", result.error)
            return None

        return result.value
