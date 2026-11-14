-- v1.4: temporary MySQL-protocol export credentials
-- Only auth_string (double SHA1 digest) is stored, with no plaintext and no reversible ciphertext.

CREATE TABLE IF NOT EXISTS export_credential (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  account_name VARCHAR(64) NOT NULL,
  auth_string CHAR(40) NOT NULL,
  token_id BIGINT UNSIGNED NOT NULL,
  datasource_id BIGINT UNSIGNED NOT NULL,
  default_database VARCHAR(128) NOT NULL,
  issued_by VARCHAR(64) NOT NULL,
  expires_at DATETIME(3) NOT NULL,
  revoked_at DATETIME(3) NULL,
  last_used_at DATETIME(3) NULL,
  created_at DATETIME(3) NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_export_account (account_name),
  KEY idx_export_token (token_id),
  KEY idx_export_expires (expires_at),
  CONSTRAINT fk_export_token FOREIGN KEY (token_id)
    REFERENCES api_token(id) ON DELETE CASCADE,
  CONSTRAINT fk_export_datasource FOREIGN KEY (datasource_id)
    REFERENCES datasource(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

ALTER TABLE audit_log ADD COLUMN credential_account VARCHAR(64) NULL AFTER token_prefix;
