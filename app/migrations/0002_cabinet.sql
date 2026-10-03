-- Kith schema, version 0002 — the cabinet: photos, groups, tags, search.
--
-- Two things to note for your own reference:
--   * `photo.data` holds the DOWNSCALED image and `photo.thumb` the list-sized copy.
--     Full-resolution originals are deliberately not kept (Q10: settled), which is what
--     keeps the whole database copyable.
--   * This migration is the first with `CREATE TRIGGER ... BEGIN ... END;` bodies, so it
--     is also the live test of the migration runner's statement splitter: a naive
--     split-on-semicolon would shred these triggers into invalid fragments.

-- ── photos ────────────────────────────────────────────────────────────────────
CREATE TABLE photo (
  id         INTEGER PRIMARY KEY,
  person_id  INTEGER NOT NULL REFERENCES person(id) ON DELETE CASCADE,
  is_primary INTEGER NOT NULL DEFAULT 0 CHECK (is_primary IN (0, 1)),
  mime       TEXT    NOT NULL,
  width      INTEGER NOT NULL,
  height     INTEGER NOT NULL,
  bytes      INTEGER NOT NULL,          -- size of `data`
  sha256     TEXT    NOT NULL,          -- of the uploaded bytes: exact-duplicate guard
  dhash      TEXT,                      -- 64-bit difference hash, hex; NULL if undecodable
  crop       TEXT,                      -- JSON {x,y,w,h} in `data` pixels, or NULL
  source     TEXT,                      -- where the photo came from
  caption    TEXT,
  data       BLOB    NOT NULL,          -- max 1024px edge, WebP
  thumb      BLOB    NOT NULL,          -- max 256px edge, WebP
  created_at TEXT    NOT NULL
) STRICT;

CREATE UNIQUE INDEX photo_sha256 ON photo(sha256);
CREATE INDEX photo_person ON photo(person_id, is_primary DESC, id);

-- ── groups (deliberately overlapping; a person may sit in many) ───────────────
CREATE TABLE grp (
  id         INTEGER PRIMARY KEY,
  name       TEXT NOT NULL UNIQUE,
  note       TEXT,
  created_at TEXT NOT NULL
) STRICT;

CREATE TABLE grp_member (
  grp_id    INTEGER NOT NULL REFERENCES grp(id) ON DELETE CASCADE,
  person_id INTEGER NOT NULL REFERENCES person(id) ON DELETE CASCADE,
  added_at  TEXT NOT NULL,
  PRIMARY KEY (grp_id, person_id)
) STRICT;

CREATE INDEX grp_member_person ON grp_member(person_id);

-- ── tags (free-form, orthogonal to groups) ────────────────────────────────────
CREATE TABLE tag (
  id         INTEGER PRIMARY KEY,
  name       TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL
) STRICT;

CREATE TABLE person_tag (
  person_id INTEGER NOT NULL REFERENCES person(id) ON DELETE CASCADE,
  tag_id    INTEGER NOT NULL REFERENCES tag(id) ON DELETE CASCADE,
  PRIMARY KEY (person_id, tag_id)
) STRICT;

-- ── full-text search over the fields you would actually type ──────────────────
-- External-content FTS5: the index stores no copy of the text, the triggers keep it
-- in sync with `person`. remove_diacritics makes "Muller" find "Müller".
CREATE VIRTUAL TABLE person_fts USING fts5(
  display_name, nickname, notes, mnemonic, where_met,
  content='person',
  content_rowid='id',
  tokenize="unicode61 remove_diacritics 2"
);

INSERT INTO person_fts (rowid, display_name, nickname, notes, mnemonic, where_met)
  SELECT id, display_name, nickname, notes, mnemonic, where_met FROM person;

CREATE TRIGGER person_fts_insert AFTER INSERT ON person BEGIN
  INSERT INTO person_fts (rowid, display_name, nickname, notes, mnemonic, where_met)
  VALUES (new.id, new.display_name, new.nickname, new.notes, new.mnemonic, new.where_met);
END;

CREATE TRIGGER person_fts_delete AFTER DELETE ON person BEGIN
  INSERT INTO person_fts (person_fts, rowid, display_name, nickname, notes, mnemonic, where_met)
  VALUES ('delete', old.id, old.display_name, old.nickname, old.notes, old.mnemonic, old.where_met);
END;

CREATE TRIGGER person_fts_update AFTER UPDATE ON person BEGIN
  INSERT INTO person_fts (person_fts, rowid, display_name, nickname, notes, mnemonic, where_met)
  VALUES ('delete', old.id, old.display_name, old.nickname, old.notes, old.mnemonic, old.where_met);
  INSERT INTO person_fts (rowid, display_name, nickname, notes, mnemonic, where_met)
  VALUES (new.id, new.display_name, new.nickname, new.notes, new.mnemonic, new.where_met);
END;
