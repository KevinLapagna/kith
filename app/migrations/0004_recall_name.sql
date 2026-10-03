-- 0004_recall_name.sql — the name you are asked becomes *resolved*, not copied.
--
-- display_name used to mean "the name you want to recall", and had to be typed even when it was
-- simply the given name. It is now an optional override: what the trainer asks for is
-- COALESCE(display_name, given_name) — see RECALL_NAME in app/repo/people.py.
--
-- Two things follow from that, and this migration applies both:
--   * a row whose display name is a single word gets it as its given name, so the form shows the
--     first name and nothing has to be retyped by hand;
--   * the search index must hold the name you would actually *type* — the resolved one — plus
--     the family name, so a surname is findable by prefix and not only through the LIKE fallback.

UPDATE person
   SET given_name = TRIM(display_name)
 WHERE (given_name IS NULL OR TRIM(given_name) = '')
   AND TRIM(display_name) <> ''
   AND INSTR(TRIM(display_name), ' ') = 0;

-- The index is external-content (content='person'), so every trigger must write the *same*
-- text the index was built from, or deletes leave ghost rows behind. `rebuild` at the end
-- re-reads the whole table through the new expression.
DROP TRIGGER IF EXISTS person_fts_insert;
DROP TRIGGER IF EXISTS person_fts_delete;
DROP TRIGGER IF EXISTS person_fts_update;

CREATE TRIGGER person_fts_insert AFTER INSERT ON person BEGIN
  INSERT INTO person_fts (rowid, display_name, nickname, notes, mnemonic, where_met)
  VALUES (
    new.id,
    COALESCE(NULLIF(TRIM(new.display_name), ''), new.given_name, '')
      || CASE WHEN new.family_name IS NULL OR TRIM(new.family_name) = ''
              THEN '' ELSE ' ' || TRIM(new.family_name) END,
    new.nickname, new.notes, new.mnemonic, new.where_met
  );
END;

CREATE TRIGGER person_fts_delete AFTER DELETE ON person BEGIN
  INSERT INTO person_fts (person_fts, rowid, display_name, nickname, notes, mnemonic, where_met)
  VALUES (
    'delete', old.id,
    COALESCE(NULLIF(TRIM(old.display_name), ''), old.given_name, '')
      || CASE WHEN old.family_name IS NULL OR TRIM(old.family_name) = ''
              THEN '' ELSE ' ' || TRIM(old.family_name) END,
    old.nickname, old.notes, old.mnemonic, old.where_met
  );
END;

CREATE TRIGGER person_fts_update AFTER UPDATE ON person BEGIN
  INSERT INTO person_fts (person_fts, rowid, display_name, nickname, notes, mnemonic, where_met)
  VALUES (
    'delete', old.id,
    COALESCE(NULLIF(TRIM(old.display_name), ''), old.given_name, '')
      || CASE WHEN old.family_name IS NULL OR TRIM(old.family_name) = ''
              THEN '' ELSE ' ' || TRIM(old.family_name) END,
    old.nickname, old.notes, old.mnemonic, old.where_met
  );
  INSERT INTO person_fts (rowid, display_name, nickname, notes, mnemonic, where_met)
  VALUES (
    new.id,
    COALESCE(NULLIF(TRIM(new.display_name), ''), new.given_name, '')
      || CASE WHEN new.family_name IS NULL OR TRIM(new.family_name) = ''
              THEN '' ELSE ' ' || TRIM(new.family_name) END,
    new.nickname, new.notes, new.mnemonic, new.where_met
  );
END;

-- The index is external-content, and FTS5's own 'rebuild' re-reads the *raw* table columns —
-- which is precisely the un-resolved display_name this migration is moving away from. So the
-- index is cleared and repopulated explicitly, through the same expression the triggers use.
INSERT INTO person_fts (person_fts) VALUES ('delete-all');

INSERT INTO person_fts (rowid, display_name, nickname, notes, mnemonic, where_met)
SELECT id,
       COALESCE(NULLIF(TRIM(display_name), ''), given_name, '')
         || CASE WHEN family_name IS NULL OR TRIM(family_name) = ''
                 THEN '' ELSE ' ' || TRIM(family_name) END,
       nickname, notes, mnemonic, where_met
FROM person;
