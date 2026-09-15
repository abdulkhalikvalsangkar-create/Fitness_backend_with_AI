-- Per-attachment OCR/LLM-cleaned text, keyed by blob_id directly.
--
-- medical_report.extracted_info (see 004) keyed this by "today's report row"
-- for the user, so a second upload on the same day silently overwrote the
-- first upload's text and extracted_info_for_attachment(first_blob_id) went
-- blank. Attachments are per-blob, so the text they carry belongs on the
-- blob row, not on a once-a-day snapshot shared by every upload that day.
ALTER TABLE blob_object
  ADD COLUMN IF NOT EXISTS extracted_text MEDIUMTEXT NULL AFTER kind;
