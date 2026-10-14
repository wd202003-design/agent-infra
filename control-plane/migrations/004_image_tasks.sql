CREATE TABLE v3_records (kind TEXT NOT NULL, id TEXT NOT NULL, root TEXT NOT NULL, state TEXT NOT NULL, created DOUBLE PRECISION NOT NULL, body TEXT NOT NULL, PRIMARY KEY(kind,id));
CREATE INDEX v3_lookup ON v3_records(kind,root,state,created);
CREATE TABLE v3_idempotency (scope TEXT NOT NULL, key TEXT NOT NULL, hash TEXT NOT NULL, response TEXT NOT NULL, PRIMARY KEY(scope,key));
INSERT INTO schema_version(version) VALUES(4);
