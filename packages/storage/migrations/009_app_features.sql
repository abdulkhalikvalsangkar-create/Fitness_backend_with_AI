-- App-facing features: product identity, reminders, data export, account deletion.

-- Product identity the app displays (name/brand were already stored; quantity
-- and the packshot URL come from the Open*Facts record and were being dropped).
ALTER TABLE product
  ADD COLUMN IF NOT EXISTS quantity VARCHAR(64) NULL AFTER category;
ALTER TABLE product
  ADD COLUMN IF NOT EXISTS image_url VARCHAR(512) NULL AFTER quantity;

-- Reminders: dosage | feeding | medication | activity | health | petcare.
-- remind_time is 'HH:mm' (24h); remind_date is set for one-off reminders and
-- days is a JSON array of ISO weekdays (1 = Monday ... 7 = Sunday) for
-- repeating ones. `time` and `date` are reserved words, hence the prefixes.
CREATE TABLE IF NOT EXISTS reminder (
  reminder_id   CHAR(32)     NOT NULL,
  user_id       VARCHAR(64)  NOT NULL,
  category      VARCHAR(16)  NOT NULL,
  enabled       TINYINT(1)   NOT NULL DEFAULT 1,
  title         VARCHAR(191) NOT NULL,
  subtitle      VARCHAR(255) NULL,
  remind_time   CHAR(5)      NOT NULL,
  remind_date   DATE         NULL,
  days          JSON         NULL,
  supplement_id VARCHAR(64)  NULL,
  pet_id        VARCHAR(64)  NULL,
  item          VARCHAR(191) NULL,
  created_at    DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at    DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (reminder_id),
  KEY ix_reminder_user (user_id, category, remind_time),
  CONSTRAINT fk_reminder_user FOREIGN KEY (user_id) REFERENCES app_user (user_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
