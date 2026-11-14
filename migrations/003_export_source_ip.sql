-- v1.6: bind temporary export credentials to a source IP
-- NULL marks a legacy credential issued before the migration. The source is not checked (it converges naturally on expiry or revocation).

ALTER TABLE export_credential
  ADD COLUMN source_ip VARCHAR(64) NULL AFTER issued_by;
