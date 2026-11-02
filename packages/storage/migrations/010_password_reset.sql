-- One-time-code password reset (forgot password).
--
-- At most one live reset per email, keyed by the SHA-256 of the lower-cased
-- address: the table never stores the address itself, and it exists whether or
-- not an account does, so the API cannot be used to discover which emails are
-- registered. Only a keyed hash of the code is stored.
CREATE TABLE IF NOT EXISTS password_reset_otp (
  email_hash        CHAR(64)    NOT NULL,
  otp_hash          CHAR(64)    NOT NULL,
  expires_at        DATETIME(3) NOT NULL,
  attempts          INT         NOT NULL DEFAULT 0,
  sends_in_window   INT         NOT NULL DEFAULT 1,
  window_started_at DATETIME(3) NOT NULL,
  last_sent_at      DATETIME(3) NOT NULL,
  verified_at       DATETIME(3) NULL,
  reset_jti         CHAR(32)    NULL,
  reset_used_at     DATETIME(3) NULL,
  created_at        DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (email_hash),
  KEY ix_password_reset_expiry (expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
