CREATE TABLE IF NOT EXISTS v2_http_keys (scope TEXT NOT NULL, key TEXT NOT NULL, hash TEXT NOT NULL, created DOUBLE PRECISION NOT NULL, PRIMARY KEY(scope,key));
CREATE TABLE IF NOT EXISTS v2_runner_event_ids (attempt_id TEXT NOT NULL REFERENCES v2_attempts(id), event_id TEXT NOT NULL, seq INTEGER NOT NULL, PRIMARY KEY(attempt_id,event_id));
INSERT INTO schema_version(version) VALUES(3) ON CONFLICT(version) DO NOTHING;
ALTER TABLE v2_idempotency ADD COLUMN secret_until DOUBLE PRECISION;
CREATE INDEX IF NOT EXISTS v2_secret_expiry ON v2_idempotency(secret_until);
CREATE TABLE IF NOT EXISTS v2_node_deletions (attempt_id TEXT PRIMARY KEY REFERENCES v2_attempts(id), acknowledged DOUBLE PRECISION NOT NULL);
ALTER TABLE v2_tasks ADD COLUMN archived INTEGER NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS v2_retention_tasks ON v2_tasks(archived,created);
ALTER TABLE v2_attempts ADD COLUMN local_content INTEGER NOT NULL DEFAULT 0;
