-- Token expiry and throttled last-used tracking.
-- Replays on every startup: ADD COLUMN tolerates errno 1060.

ALTER TABLE api_token ADD COLUMN expires_at DATETIME(3) NULL;
ALTER TABLE api_token ADD COLUMN last_used_at DATETIME(3) NULL;
