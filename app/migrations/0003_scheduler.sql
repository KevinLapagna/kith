-- Kith schema, version 0003.
--
-- M2 scope: the trainer's storage.
--   * `card`      — one per (person, direction), owned by py-fsrs, state as JSON.
--   * `session` / `session_item` — the queue lives server-side, which is what makes a
--     session survive a closed tab, a locked phone or a container restart.
--   * `review`    — append-only log: stats, the confusion matrix, and (later) FSRS
--     parameter optimisation all read this table and nothing else.
--
-- `due` is denormalised onto `card` so "what is due" is one indexed query instead of a
-- scan through the FSRS JSON. The scheduler is its only writer, and a test asserts the
-- JSON and the column never disagree.

-- ── one trainable card per (person, direction) ───────────────────────────────
CREATE TABLE card (
  id         INTEGER PRIMARY KEY,
  person_id  INTEGER NOT NULL REFERENCES person(id) ON DELETE CASCADE,
  direction  TEXT NOT NULL CHECK (direction IN ('face_to_name','name_to_face')),
  fsrs       TEXT NOT NULL,            -- json.dumps(Card.to_dict(), default=str)
  due        TEXT NOT NULL,            -- denormalised from `fsrs`, indexed
  stability  REAL,
  difficulty REAL,
  state      INTEGER,
  reps       INTEGER NOT NULL DEFAULT 0,
  lapses     INTEGER NOT NULL DEFAULT 0,
  suspended  INTEGER NOT NULL DEFAULT 0,   -- a leech you asked to stop asking
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (person_id, direction)
) STRICT;

CREATE INDEX card_due_idx    ON card (due) WHERE suspended = 0;
CREATE INDEX card_person_idx ON card (person_id, direction);

-- ── a training run ───────────────────────────────────────────────────────────
CREATE TABLE session (
  id            INTEGER PRIMARY KEY,
  started_at    TEXT NOT NULL,
  ended_at      TEXT,
  grp_ids       TEXT,                  -- json array of group ids; NULL = everyone
  person_ids    TEXT,                  -- json array, for "drill my mistakes from that session"
  modes         TEXT NOT NULL,         -- json array of mode keys, in preference order
  target_kind   TEXT NOT NULL DEFAULT 'everything'
                CHECK (target_kind IN ('everything','groups','due','people')),
  new_budget    INTEGER NOT NULL DEFAULT 0,   -- never-seen people to introduce
  retire_count  INTEGER NOT NULL DEFAULT 1,   -- correct recalls that retire an item
  planned_items INTEGER NOT NULL DEFAULT 0,
  answered      INTEGER NOT NULL DEFAULT 0,
  correct       INTEGER NOT NULL DEFAULT 0
) STRICT;

-- ── the server-side queue ────────────────────────────────────────────────────
-- `seq` is insertion order and never changes; `position` is the live queue order and is
-- deliberately *not* unique, because a requeued item has to slot back in mid-queue. Two
-- columns rather than one because a UNIQUE ordering column cannot be shifted without
-- transiently colliding with the row it is moving past.
CREATE TABLE session_item (
  id            INTEGER PRIMARY KEY,
  session_id    INTEGER NOT NULL REFERENCES session(id) ON DELETE CASCADE,
  seq           INTEGER NOT NULL,
  position      INTEGER NOT NULL DEFAULT 0,
  card_id       INTEGER REFERENCES card(id) ON DELETE CASCADE,
  new_person_id INTEGER REFERENCES person(id) ON DELETE CASCADE,
  mode          TEXT NOT NULL,
  state         TEXT NOT NULL DEFAULT 'pending'
                CHECK (state IN ('pending','answered','requeued','skipped')),
  requeues      INTEGER NOT NULL DEFAULT 0,
  correct_count INTEGER NOT NULL DEFAULT 0,
  UNIQUE (session_id, seq)
) STRICT;

CREATE INDEX session_item_open ON session_item (session_id, state, position);

-- ── the append-only review log ───────────────────────────────────────────────
CREATE TABLE review (
  id               INTEGER PRIMARY KEY,
  card_id          INTEGER NOT NULL REFERENCES card(id) ON DELETE CASCADE,
  session_id       INTEGER REFERENCES session(id) ON DELETE SET NULL,
  session_item_id  INTEGER REFERENCES session_item(id) ON DELETE SET NULL,
  reviewed_at      TEXT NOT NULL,
  rating           INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 4),
  derived          INTEGER NOT NULL DEFAULT 1,   -- 0 = you overrode the derived rating
  mode             TEXT NOT NULL,
  correct          INTEGER NOT NULL,
  no_idea          INTEGER NOT NULL DEFAULT 0,   -- "never seen it" ≠ a wrong answer
  hint_level       INTEGER NOT NULL DEFAULT 0,
  latency_ms       INTEGER,
  chosen_person_id INTEGER REFERENCES person(id) ON DELETE SET NULL,  -- the lure you tapped
  confidence       INTEGER,                      -- optional 1..3, for calibration
  elapsed_days     REAL,
  scheduled_days   REAL,
  duration_ms      INTEGER
) STRICT;

CREATE INDEX review_card_time ON review (card_id, reviewed_at);
CREATE INDEX review_confusion ON review (card_id, chosen_person_id) WHERE correct = 0;
CREATE INDEX review_session   ON review (session_id, reviewed_at);

-- ── trainer knobs (values are the documented defaults) ───────────────────────
INSERT INTO setting (key, value, updated_at) VALUES
  ('retire_count',     '1',    strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
  ('interleave_window','4',    strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
  ('maintenance_floor','0.10', strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
  ('slow_factor',      '1.5',  strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
  ('fast_factor',      '0.6',  strftime('%Y-%m-%dT%H:%M:%SZ', 'now'));
