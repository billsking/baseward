-- v1.10: exchange-kind tokens and short-lived actor-bound MCP credentials.
-- Replays on every startup: ADD COLUMN tolerates errno 1060, CREATE TABLE tolerates 1050, CREATE INDEX tolerates 1061.

ALTER TABLE api_token
  ADD COLUMN token_kind VARCHAR(16) NOT NULL DEFAULT 'access';

CREATE TABLE IF NOT EXISTS short_credential (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  credential_hash CHAR(64) NOT NULL,
  credential_prefix VARCHAR(16) NOT NULL,
  parent_token_id BIGINT UNSIGNED NOT NULL,
  actor_provider VARCHAR(32) NOT NULL,
  actor_id VARCHAR(128) NOT NULL,
  actor_name VARCHAR(128) NULL,
  scope_json TEXT NULL,
  source_ip VARCHAR(64) NULL,
  expires_at DATETIME(3) NOT NULL,
  revoked_at DATETIME(3) NULL,
  last_used_at DATETIME(3) NULL,
  created_at DATETIME(3) NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_short_hash (credential_hash),
  KEY idx_short_parent (parent_token_id),
  KEY idx_short_expires (expires_at),
  KEY idx_short_actor (actor_provider, actor_id),
  CONSTRAINT fk_short_parent FOREIGN KEY (parent_token_id)
    REFERENCES api_token(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;