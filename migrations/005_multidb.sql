-- v1.8 multi-database: exposed database list and the audit database dimension.
-- This file replays on every startup: CREATE TABLE IF NOT EXISTS tolerates errno 1050,
-- ALTER TABLE ADD COLUMN tolerates errno 1060.
-- The INSERT IGNORE below is not just an idempotency guard. It also backfills "legacy datasources
-- missing a default-database row" and must not be rewritten to compute results in the app first.

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

INSERT IGNORE INTO datasource_database
  (datasource_id, name, is_read_only, created_at, updated_at)
SELECT id, default_database, is_read_only, UTC_TIMESTAMP(3), UTC_TIMESTAMP(3)
FROM datasource;

ALTER TABLE audit_log
  ADD COLUMN database_name VARCHAR(128) NULL
  COMMENT '本次执行的有效默认库；跨库限定名见 sql_text';
