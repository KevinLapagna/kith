-- Kith schema, version 0001.
--
-- M0 scope: prove the storage layer end to end. This migration creates the
-- metadata table, the settings store, and the one entity everything else hangs
-- off — `person`. Photos, groups, tags, scheduler cards and review history arrive
-- in later numbered migrations; this file is never edited again once applied.

-- ── bookkeeping ──────────────────────────────────────────────────────────────
CREATE TABLE app_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
) STRICT;

INSERT INTO app_meta (key, value) VALUES
  ('app',        'kith'),
  ('created_at', strftime('%Y-%m-%dT%H:%M:%SZ', 'now'));

-- ── settings (single row per key; no config file to drift out of sync) ───────
CREATE TABLE setting (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  updated_at TEXT NOT NULL
) STRICT;

INSERT INTO setting (key, value, updated_at) VALUES
  ('desired_retention', '0.90', strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
  ('session_minutes',   '10',   strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
  ('new_face_budget',   '3',    strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
  ('modes_enabled',     '["mc4","recall_self","match3"]', strftime('%Y-%m-%dT%H:%M:%SZ', 'now'));

-- ── people ───────────────────────────────────────────────────────────────────
-- display_name is what you actually want to recall, which is not necessarily the
-- legal name: "Andi" and "Andrea Weber" can both be right depending on context.
CREATE TABLE person (
  id            INTEGER PRIMARY KEY,
  display_name  TEXT NOT NULL,
  given_name    TEXT,
  family_name   TEXT,
  nickname      TEXT,
  pronunciation TEXT,            -- "AH-na", or a path to a voice clip (M5)
  mnemonic      TEXT,            -- your own encoding device
  feature_note  TEXT,            -- "gap between front teeth"
  where_met     TEXT,
  met_on        TEXT,            -- ISO date
  notes         TEXT,
  status        TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'archived', 'paused')),
  paused_until  TEXT,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
) STRICT;

CREATE INDEX person_status_idx ON person (status, display_name);
