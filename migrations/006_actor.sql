-- v1.9: actor identity (P1, self-asserted trust level).
-- Replays on every startup: ADD COLUMN tolerates errno 1060, CREATE INDEX tolerates 1061.

ALTER TABLE api_token
  ADD COLUMN actor_policy VARCHAR(16) NOT NULL DEFAULT 'ignore';

ALTER TABLE audit_log ADD COLUMN actor_provider VARCHAR(32) NULL;
ALTER TABLE audit_log ADD COLUMN actor_id VARCHAR(128) NULL;
ALTER TABLE audit_log ADD COLUMN actor_name VARCHAR(128) NULL;
ALTER TABLE audit_log ADD COLUMN actor_trust VARCHAR(16) NULL;

CREATE INDEX idx_audit_actor ON audit_log (actor_provider, actor_id, created_at);