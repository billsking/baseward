-- v1.7: move the source-IP allowlist to the admin "Settings" page
-- setting_value must hold a whole CIDR list (max 1024 chars). 255 would be truncated by MySQL on save and fail.
-- The allowlist gets no seed row: only a missing row means "never saved, decide by env var".
-- Inserting an empty row would configure that entrypoint as "deny all".

ALTER TABLE system_setting
  MODIFY COLUMN setting_value VARCHAR(1024) NOT NULL;
