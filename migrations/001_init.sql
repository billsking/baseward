CREATE TABLE IF NOT EXISTS admin_user (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  username VARCHAR(64) NOT NULL,
  password_hash VARCHAR(100) NOT NULL,
  session_version INT UNSIGNED NOT NULL DEFAULT 0,
  created_at DATETIME(3) NOT NULL,
  updated_at DATETIME(3) NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_admin_username (username)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS datasource (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  name VARCHAR(64) NOT NULL,
  host VARCHAR(255) NOT NULL,
  port INT UNSIGNED NOT NULL DEFAULT 3306,
  username VARCHAR(128) NOT NULL,
  password_cipher TEXT NOT NULL,
  default_database VARCHAR(128) NOT NULL,
  is_read_only TINYINT(1) NOT NULL DEFAULT 1,
  enabled TINYINT(1) NOT NULL DEFAULT 1,
  remark VARCHAR(255) NULL,
  created_at DATETIME(3) NOT NULL,
  updated_at DATETIME(3) NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_datasource_name (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS datasource_database (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  datasource_id BIGINT UNSIGNED NOT NULL,
  name VARCHAR(128) NOT NULL,
  is_read_only TINYINT(1) NOT NULL DEFAULT 1,
  created_at DATETIME(3) NOT NULL,
  updated_at DATETIME(3) NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_datasource_database (datasource_id, name),
  CONSTRAINT fk_datasource_database_ds FOREIGN KEY (datasource_id)
    REFERENCES datasource(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS masking_rule (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  datasource_id BIGINT UNSIGNED NOT NULL,
  database_name VARCHAR(128) NOT NULL,
  table_name VARCHAR(128) NOT NULL,
  column_name VARCHAR(128) NOT NULL,
  strategy VARCHAR(16) NOT NULL,
  mask_pattern VARCHAR(16) NULL,
  created_at DATETIME(3) NOT NULL,
  updated_at DATETIME(3) NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_masking_rule (datasource_id, database_name, table_name, column_name),
  KEY idx_masking_ds (datasource_id),
  CONSTRAINT fk_masking_ds FOREIGN KEY (datasource_id)
    REFERENCES datasource(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS api_token (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  user_name VARCHAR(64) NOT NULL,
  token_hash CHAR(64) NOT NULL,
  token_prefix VARCHAR(16) NOT NULL,
  enabled TINYINT(1) NOT NULL DEFAULT 1,
  created_at DATETIME(3) NOT NULL,
  disabled_at DATETIME(3) NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_token_hash (token_hash),
  KEY idx_token_user (user_name, enabled)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS token_scope (
  token_id BIGINT UNSIGNED NOT NULL,
  scope_json MEDIUMTEXT NOT NULL,
  created_at DATETIME(3) NOT NULL,
  updated_at DATETIME(3) NOT NULL,
  PRIMARY KEY (token_id),
  CONSTRAINT fk_token_scope_token FOREIGN KEY (token_id)
    REFERENCES api_token(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS audit_log (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  request_id CHAR(36) NOT NULL,
  event_type VARCHAR(32) NOT NULL,
  user_name VARCHAR(64) NOT NULL,
  token_id BIGINT UNSIGNED NULL,
  token_prefix VARCHAR(16) NULL,
  datasource_id BIGINT UNSIGNED NULL,
  datasource_name VARCHAR(64) NULL,
  database_name VARCHAR(128) NULL,
  tool VARCHAR(32) NULL,
  sql_text MEDIUMTEXT NULL,
  status VARCHAR(16) NOT NULL,
  deny_reason VARCHAR(512) NULL,
  error_message TEXT NULL,
  duration_ms INT UNSIGNED NULL,
  row_count BIGINT NULL,
  client_ip VARCHAR(64) NULL,
  created_at DATETIME(3) NOT NULL,
  finished_at DATETIME(3) NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_audit_request (request_id),
  KEY idx_audit_created (created_at, id),
  KEY idx_audit_user (user_name, created_at),
  KEY idx_audit_ds (datasource_name, created_at),
  KEY idx_audit_status (status, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS system_setting (
  setting_key VARCHAR(64) NOT NULL,
  setting_value VARCHAR(1024) NOT NULL,
  updated_at DATETIME(3) NOT NULL,
  PRIMARY KEY (setting_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

INSERT IGNORE INTO system_setting (setting_key, setting_value, updated_at)
VALUES ('audit_retention_days', '180', UTC_TIMESTAMP(3));
