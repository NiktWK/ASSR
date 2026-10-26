-- Initial equivalent of SQLAlchemy metadata. Run against an empty PostgreSQL database.
-- Application bootstrap uses CREATE TABLE IF NOT EXISTS via metadata.create_all.
BEGIN;
CREATE TABLE IF NOT EXISTS campaigns (
  id VARCHAR(36) PRIMARY KEY,
  revision INTEGER NOT NULL,
  body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  seq SERIAL PRIMARY KEY,
  campaign_id VARCHAR(36),
  body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_campaign_id ON events(campaign_id);
CREATE TABLE IF NOT EXISTS requests (
  key VARCHAR(180) PRIMARY KEY,
  fingerprint VARCHAR(64),
  response TEXT
);
CREATE TABLE IF NOT EXISTS locks (
  id INTEGER PRIMARY KEY,
  value INTEGER
);
INSERT INTO locks(id,value) VALUES (1,0) ON CONFLICT(id) DO NOTHING;
COMMIT;
