-- Backend refresh-token sessions. Only SHA-256 hashes are stored.
CREATE TABLE IF NOT EXISTS auth_session (
  session_id       CHAR(36) NOT NULL,
  user_id          VARCHAR(64) NOT NULL,
  refresh_hash     CHAR(64) NOT NULL,
  device_id        VARCHAR(191) NULL,
  device_name      VARCHAR(191) NULL,
  platform         VARCHAR(32) NULL,
  app_version      VARCHAR(32) NULL,
  created_at       DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  last_used_at     DATETIME(3) NULL,
  expires_at       DATETIME(3) NOT NULL,
  revoked_at       DATETIME(3) NULL,
  replaced_by      CHAR(36) NULL,
  PRIMARY KEY (session_id),
  UNIQUE KEY uq_auth_refresh_hash (refresh_hash),
  KEY ix_auth_session_user (user_id),
  CONSTRAINT fk_auth_session_user FOREIGN KEY (user_id) REFERENCES app_user(user_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
