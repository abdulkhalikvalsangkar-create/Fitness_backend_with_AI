-- Admin identity, security sessions, and chemical review control.
-- Passwords are never stored in plaintext. Provision password_hash through a
-- setup script or admin provisioning flow; JWTs remain signed with JWT_SECRET
-- and are not stored in this table.

CREATE TABLE IF NOT EXISTS admin_user (
  admin_id              VARCHAR(64)  NOT NULL,
  user_id               VARCHAR(64)  NOT NULL,
  email                 VARCHAR(191) NOT NULL,
  password_hash         VARCHAR(255) NOT NULL,
  status                VARCHAR(16)  NOT NULL DEFAULT 'active', -- active | disabled | locked
  scopes                JSON         NULL,
  token_version         BIGINT       NOT NULL DEFAULT 1,
  failed_login_attempts INT          NOT NULL DEFAULT 0,
  locked_until          DATETIME(3)  NULL,
  last_login_at         DATETIME(3)  NULL,
  password_changed_at   DATETIME(3)  NULL,
  created_at            DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at            DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (admin_id),
  UNIQUE KEY uq_admin_user_id (user_id),
  UNIQUE KEY uq_admin_email (email),
  KEY ix_admin_status (status),
  CONSTRAINT chk_admin_status CHECK (status IN ('active', 'disabled', 'locked'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Refresh tokens are stored only as hashes, matching auth_session.
CREATE TABLE IF NOT EXISTS admin_session (
  session_id       CHAR(36)     NOT NULL,
  admin_id         VARCHAR(64)  NOT NULL,
  refresh_hash     CHAR(64)     NOT NULL,
  device_id        VARCHAR(191) NULL,
  device_name      VARCHAR(191) NULL,
  platform         VARCHAR(32)  NULL,
  app_version      VARCHAR(32)  NULL,
  created_at       DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  last_used_at     DATETIME(3)  NULL,
  expires_at       DATETIME(3)  NOT NULL,
  revoked_at       DATETIME(3)  NULL,
  replaced_by      CHAR(36)     NULL,
  PRIMARY KEY (session_id),
  UNIQUE KEY uq_admin_refresh_hash (refresh_hash),
  KEY ix_admin_session_admin (admin_id),
  KEY ix_admin_session_expiry (expires_at),
  CONSTRAINT fk_admin_session_admin FOREIGN KEY (admin_id)
    REFERENCES admin_user(admin_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS admin_setting (
  setting_key VARCHAR(64)  NOT NULL,
  value       VARCHAR(255) NOT NULL,
  updated_by  VARCHAR(64)  NULL,
  updated_at  DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
                           ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (setting_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

INSERT IGNORE INTO admin_setting (setting_key, value, updated_by)
VALUES ('review_status_control', 'manual', 'migration')
