-- Add extracted document text to medical reports.
-- This is a separate migration because 001_init may already be recorded as applied.
ALTER TABLE medical_report
  ADD COLUMN IF NOT EXISTS extracted_info MEDIUMTEXT NULL AFTER source_blob_id;
