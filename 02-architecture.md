# Kith — Architecture & Tech Stack
## Document 2 of 2: How It Is Built

> **Companion document:** `01-app-description.md` (features, look & feel, learning design).
> **Status:** Draft v0.2 — decisions in §19 settled with Kevin on 2026-10-01 (all defaults
> accepted; the UI is **English only** and the runtime is **Podman + Quadlet**).

---

## 1. Design goals and constraints

| # | Constraint | Consequence |
|---|---|---|
| C1 | **Container-native** (your requirement). Ships as an image; the host needs nothing but a container runtime. | No host Python/uv/pip, no host venv, no host libraries. All dependencies pinned in the image. |
| C2 | **All data, including images, in a single SQLite file.** | Photos are BLOBs. The data directory holds exactly one `.sqlite` (+ `-wal`/`-shm` while running). Coping that one file *is* the backup. |
| C3 | Single user, private. | No accounts/roles, no multi-tenancy, no telemetry, no outbound network calls at runtime. One optional password gate. |
| C4 | Point-and-click / drag; typing optional. | The trainer is a small hand-written JS island — not a form-heavy framework screens stack. |
| C5 | No frontend build step. | Server-rendered templates + HTMX + plain CSS/ES modules. Nothing to `npm install`, ever. |
| C6 | Boring, conventional, debuggable. | FastAPI + SQLite + Jinja/HTMX. Every piece is the obvious choice, and I can explain each one. |
| C7 | Runs for years unattended. | Pinned dependencies, migrations on boot, a healthcheck, `Restart=always` on the Quadlet unit, and no scheduler that silently degrades. |

**Explicit non-goals for v1:** face recognition, multi-device sync, native apps, encryption at
rest (see §18 stretch), Kubernetes, any LLM in the runtime path.

---

## 2. Verified library behaviour (probed, not assumed)

I checked the two libraries the design depends on, in a **throwaway probe environment** —
purely to pin down their APIs so this document is accurate. The app is not coupled to that
environment in any way; the same versions are pinned inside the image (§4).

**`fsrs` 6.3.2** (py-fsrs, the reference Python implementation of FSRS-6):

* Exports: `Scheduler`, `Card`, `Rating`, `ReviewLog`, `State`.
* Core loop: `scheduler.review_card(card, rating, review_datetime, review_duration) -> (card, review_log)`,
  with `Rating.Again|Hard|Good|Easy` = 1|2|3|4.
* `scheduler.get_card_retrievability(card, now) -> float` — this is what the item-selection
  engine uses to find "what you are about to forget".
* `Card` carries `card_id, state, step, stability, difficulty, due, last_review`. No custom
  FSRS parameters needed for a single user; `Scheduler(desired_retention=0.9)` is enough.
* Observed interval behaviour on a fresh card (sanity check, matches FSRS expectations):
  `Again` → +1 min (Step 0, Learning) · `Good` → +10 min (Learning) · `Easy` → +7 days
  (Review). So a brand-new face is *not* dumped into a week-long interval by accident.
* **Pitfall found by probing (important):** `Card.from_dict()` calls
  `datetime.fromisoformat(source_dict["due"])`, i.e. it expects **strings**, not `datetime`
  objects. The correct persistence round-trip is
  `text = json.dumps(card.to_dict(), default=str)` → store → `Card.from_dict(json.loads(text))`.
  Handing it real `datetime` objects raises `TypeError`. This is exactly the kind of thing
  that silently corrupts a scheduler, so §15 has a dedicated regression test for it.

**`Pillow` 12.3.0** — used for the import pipeline (EXIF orientation, downscale, thumbnails,
EXIF strip, perceptual hash). Pure-Python stack, no system libraries needed beyond what
`python:3.13-slim` provides.

---

## 3. Stack decision, with the alternatives and why they lost

| Layer | Choice | Pinned | Rejected alternatives & why |
|---|---|---|---|
| Language | Python 3.13 | `python:3.13-slim` | Node/Go would be fine; Python keeps it readable and matches your other tooling. |
| Container runtime | **Podman + Quadlet** (declarative `.container` → real systemd unit) | `podman` on the host | *Docker + Compose* — your call (Q9, 2026-10-01): Quadlet is daemonless, rootless, and managed by the same systemd tooling as your other units; `loginctl enable-linger` makes it boot-persistent, `podman auto-update` handles image updates later. No `compose.yaml` needed. |
| Web framework | **FastAPI** + Uvicorn | `fastapi`, `uvicorn[standard]` | *Flask* — no async, weaker typing. *FastHTML* — clever but more exotic than "conventional". *Django* — an admin framework for one user is absurd overkill. |
| Templating | **Jinja2** server-rendered | `jinja2` | SPA (React/Svelte) needs a build step → violates C5. |
| Interactivity | **HTMX** for CRUD + one hand-written `trainer.js` for the exercise screens | `htmx.min.js` vendored locally (~14 KB) | Alpine/Stimulus add concepts without removing the need for custom drag code. Trainer state is genuinely stateful → a tiny explicit state machine beats sprinkling attributes. |
| Database | **SQLite**, BLOBs, WAL, `STRICT` tables | stdlib `sqlite3` | Filesystem-for-images: loses the C2 single-file promise, and SQLite is *faster* than the filesystem for small blobs (~35 % faster reads, ~20 % less space for 10 KB blobs, per sqlite.org). Postgres: a server for one user, plus a second thing to back up. |
| Scheduling | **py-fsrs (FSRS-6)** | `fsrs==6.3.2` | *Own SM-2*: I would be re-implementing a worse algorithm. *Leitner boxes*: simpler but no per-item memory model, so no "surprise me with what I'm about to forget". FSRS is the current state of the art and has a Python reference implementation. |
| Images | **Pillow** | `pillow==12.3.0` | ImageMagick/libvips: extra system deps inside the image for no gain at this scale. |
| CSS | **Hand-written CSS with custom properties**, one file | vendored | Tailwind CDN violates offline-first; Tailwind build violates C5. |
| Scheduling of *items within a session* | **My own selection engine** (§9) on top of FSRS | — | FSRS answers "when is this card due"; it does **not** answer "which of these 40 due cards deserves my next 30 seconds". That is the part you asked for, and it is the app's intellectual core. |
| Auth | stdlib `hashlib.scrypt` session cookie | — | Argon2/bcrypt = extra dependency for one user. Not needed unless globally reachable — and even then, scrypt + HTTPS is solid. |
| Tests | pytest (+ `httpx`/TestClient), Playwright for the drag modes | `pytest`, `playwright` (dev only) | — |

---

## 4. Container architecture

### 4.1 `Containerfile` (Podman's name for a Dockerfile; `podman build` reads either)

```dockerfile
# syntax=docker/dockerfile:1
FROM python:3.13-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    KITH_DATA_DIR=/data \
    KITH_PORT=8080

WORKDIR /app

# Dependencies first → layer cache survives app edits
COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt   # lockfile with hashes

COPY app/ ./app/
COPY static/ ./static/
COPY templates/ ./templates/
COPY migrations/ ./migrations/

# Non-root, with a fixed uid so the bind-mounted data dir has predictable ownership
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin kith \
 && mkdir -p /data && chown kith:kith /data
USER kith

VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/health').status==200 else 1)"

ENTRYPOINT ["/app/entrypoint.sh"]   # preflight the data dir, then exec uvicorn
                                    # (migrations run in the app lifespan, not here)
```

* `requirements.txt` is a **locked** file (`uv pip compile --generate-hashes`) — the image is
  reproducible and the app never resolves dependencies at runtime. `requirements.in` holds the
  handful of direct dependencies; only the compiled file is installed.
* `requirements.txt`: `fastapi==0.142.2`, `uvicorn[standard]==0.54.0`, `jinja2`, `python-multipart`,
  `pillow==12.3.0`, `fsrs==6.3.2` (all transitive deps pinned and hashed).
* `entrypoint.sh` does one thing: verify the data directory exists and is writable by the
  running uid, and fail with a message that names §16 if not. It then `exec`s
  `uvicorn app.main:app --host 0.0.0.0 --port "$KITH_PORT" --proxy-headers`. `exec` so PID 1
  is uvicorn and signals work; `--proxy-headers` because Caddy is in front.
  **Migrations deliberately do not run here** — they run in the application lifespan, so the
  container start path and the test suite exercise the identical code path (M0 correction).
* A non-root user with **uid 10001** is deliberate: bind-mounted data directories get
  predictable ownership, and the container never runs as root (see the pitfall in §16).
* **Build with `--format docker`.** Podman 5 builds OCI-format images by default and in that
  format the `HEALTHCHECK` above is *silently ignored* (it printed a warning only because the
  build was watched). The Quadlet unit sets `HealthCmd=` anyway, so this is about the image
  being self-describing rather than about the deployment working.

### 4.2 Quadlet unit (Podman, systemd-native)

**Decision (Q9, 2026-10-01): Podman + Quadlet, not Docker Compose.** Quadlet turns a
declarative `.container` file into a genuine systemd service at boot — daemonless, rootless,
and managed with the same `systemctl --user` tooling as everything else on the host.

```ini
# ~/.config/containers/systemd/kith.container   (rootless, runs as the barn user)
[Unit]
Description=Kith — name & face trainer
After=network-online.target

[Container]
Image=localhost/kith:0.1
ContainerName=kith
PublishPort=127.0.0.1:8080:8080          # loopback only; Caddy sits in front
Volume=/srv/kith/data:/data:Z            # :Z = SELinux relabel (the host is Fedora)
UserNS=keep-id:uid=10001,gid=10001         # map the host user onto the image's uid 10001
EnvironmentFile=%h/.config/kith/kith.env
HealthCmd=python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/health').status==200 else 1)"
HealthInterval=30s
HealthStartPeriod=10s
HealthOnFailure=kill

[Service]
Restart=always
TimeoutStartSec=300                      # first start applies migrations

[Install]
WantedBy=default.target
```

```bash
# build, install, run
podman build -t localhost/kith:0.1 -f Containerfile .
mkdir -p ~/.config/containers/systemd ~/.config/kith
cp kith.container ~/.config/containers/systemd/
chmod 600 ~/.config/kith/kith.env        # KITH_AUTH=on, KITH_PASSWORD_HASH=…
systemctl --user daemon-reload && systemctl --user start kith
loginctl enable-linger barn              # survive logout / restart
```

* **Rootless as `barn`** — no service account, no root, and the data directory is yours.
  The unit uses `UserNS=keep-id:uid=10001,gid=10001`, which maps *your* host user onto the
  uid the image runs as (10001), so the bind-mounted `/data` is writable without the app
  running as root. Plain `keep-id` is **not** enough when the image sets `USER 10001`:
  it maps your uid to itself, so the process would run as an unmapped 10001 and lose access
  to the mount. **Verify this at M0** (`podman exec kith id` and a write test on `/data`)
  before trusting migrations with it: a UID mismatch shows up later as `attempt to write a
  readonly database`, which is a confusing way to learn about user namespaces.
* **Quadlet vs Compose:** a Compose file is a second, parallel source of truth for the same
  service. Since the host already runs Quadlet units, adding a compose shim would be a
  gratuitous extra concept for one container with one volume and one port.

The image is identical in both cases; only where it is published differs:

```
(A) Local only                        (B) On the Barn host, reachable from your phone
┌───────────────────────┐             ┌────────────────────────────────────────────┐
│ your machine          │             │ Barn host (pantheon)                       │
│ ┌───────────────────┐ │             │ ┌──────────────┐   ┌───────────────────┐   │
│ │ kith container    │ │             │ │ Caddy (host) │──▶│ kith container    │   │
│ │ 127.0.0.1:8080    │ │             │ │ kith.barn.   │   │ 127.0.0.1:8080    │   │
│ └─────────┬─────────┘ │             │ │ opq.ch       │   └─────────┬─────────┘   │
│  (podman run)         │             │ └──────────────┘   Quadlet unit (systemd)   │
│      ./data/kith.sqlite             │                     /srv/kith/data (bind)   │
└───────────────────────┘             └────────────────────────────────────────────┘
```

For (B) the Caddy site is a bare reverse proxy **with no Caddy auth block**, because the app
brings its own login — your stated preference, and the pattern recorded in the
`barn-host-webapp-deploy` skill:

```caddyfile
kith.barn.opq.ch {
    reverse_proxy 127.0.0.1:8080
}
```

Unlike the Mnemosyne dashboard deploy, Kith needs **no host filesystem access at all** — its
data lives in its own directory. That makes it the simplest possible host deployment: one
image, one Quadlet unit, one directory, one Caddy stanza. (Same skill's warnings still apply:
firewall `http`/`https` must be permanent, and do not diagnose connectivity from inside a
container — `*.opq.ch` resolves to loopback there, so container-side curls are meaningless.)

### 4.3 Why bind mount and not a named volume

A named volume is "cleaner" but hides the single file that is your entire data ownership.
`/srv/kith/data/kith.sqlite` can be copied, inspected with `sqlite3`, put in Dropbox, or
handed to `sqlite3 .dump` — no container command needed. That directly serves C2.

---

## 5. Repository layout

```
kith/
├── 01-app-description.md          # doc 1
├── 02-architecture.md             # doc 2 (this file)
├── Containerfile                  # podman build -f Containerfile
├── entrypoint.sh                  # migrations, then exec uvicorn
├── kith.container                 # Quadlet unit (Podman → systemd)
├── kith.env.example               # KITH_AUTH, KITH_PASSWORD_HASH, TZ
├── .containerignore               # keeps data/ out of the build context
├── requirements.in                # direct deps; requirements.txt is generated
├── requirements.txt               # locked, hashed
├── requirements-dev.txt
├── pyproject.toml                 # pytest config only (no packaging)
├── Makefile                       # make dev / test / build / release
├── app/
│   ├── main.py                    # app factory, login gate middleware, health, status
│   ├── config.py                  # env → settings dataclass
│   ├── db.py                      # connection, WAL pragmas, migrations runner
│   ├── auth.py                    # scrypt hash/verify, signed session cookie, no user table
│   ├── cli.py                     # operator CLI, run inside the container (hash-password)
│   ├── migrations/
│   │   ├── 0001_init.sql
│   │   ├── 0002_cabinet.sql       # photo, grp, grp_member, tag, person_tag + FTS triggers
│   │   ├── 0003_scheduler.sql     # card, review, session, session_item, settings
│   │   ├── 0004_recall_name.sql   # the asked name becomes resolved, not copied
│   │   └── 0005_match_board.sql   # M4b: a board stores its roster on the item
│   ├── images.py                  # import pipeline: EXIF apply→strip, resize, thumb, dhash
│   ├── selection.py               # M3: scoring, sampling, the maintenance floor, the spread
│   ├── trainer.py                 # M2: build a session, ask, grade, requeue, summarise
│   ├── repo/                      # SQL access, one module per aggregate; dicts in/out
│   │   ├── __init__.py
│   │   ├── people.py              # CRUD, status filter, FTS search, stats
│   │   ├── photos.py              # EXIF-aware insert, listings, primary promotion, bytes,
│   │   │                          #   similar() — the Hamming search queue and mc4 share
│   │   ├── groups.py              # CRUD, membership, per-person group set
│   │   └── cards.py, reviews.py, sessions.py, settings.py   # M2: the scheduler's tables
│   ├── modes/                     # M2/M4: base.py + study, mc4, recall_self, reverse, listen,
│   │                              #   confusion, match (match3/match6) — the plugin point
│   ├── ladder.py                  # M4: two ladders, one per card direction
│   └── routers/
│       ├── pages.py               # cabinet, person detail/form, photo upload, groups
│       ├── auth_routes.py         # login, logout, the gate's own pages
│       └── train.py               # M2: start / ask / answer / summary
├── templates/                     # Jinja2: layout, dashboard, cabinet, person_detail,
│                                  #   person_form, _person_fields, groups, group_detail,
│                                  #   login, missing
├── static/
│   ├── css/kith.css
│   ├── css/match.css              # M4b: the board's faces, name chips and drag ghost
│   └── js/                        # live-search.js (M1) — swaps #results as you type;
│                                  #   trainer.js (M2: exercise state machine);
│                                  #   match.js (M4b: the board — drag and tap-tap)
├── scripts/
│   └── set-password.sh            # sets the gate's password without hand-editing the env
├── deploy/
│   └── kith.barn.opq.ch.caddyfile # the Caddy site: a bare reverse_proxy, no basic_auth
├── tests/                         # test_health, test_migrations, test_cabinet, test_config,
│                                  #   test_trainer, test_selection, test_match
└── locales/                       # M5: en.json (+ de.json as a drop-in)
```

---

## 6. Data model

`PRAGMA journal_mode=WAL`, `PRAGMA foreign_keys=ON`, `PRAGMA busy_timeout=5000`,
`PRAGMA synchronous=NORMAL`. All tables `STRICT`. All timestamps **UTC ISO-8601 strings**
(display timezone applied at render time). Schema version in `PRAGMA user_version`.

```sql
-- ── people ────────────────────────────────────────────────────────────────
CREATE TABLE person (
  id            INTEGER PRIMARY KEY,
  display_name  TEXT NOT NULL,          -- optional override: what you are asked for; '' = use given_name
  given_name    TEXT, family_name TEXT, nickname TEXT,
  pronunciation TEXT,                   -- "AH-na" or a path to a voice clip
  mnemonic      TEXT,                   -- your own encoding
  feature_note  TEXT,                   -- "gap between front teeth"
  where_met     TEXT, met_on TEXT,       -- context; date ISO
  notes         TEXT,
  status        TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active','archived','paused')),
  paused_until  TEXT,
  created_at    TEXT NOT NULL, updated_at TEXT NOT NULL
) STRICT;

-- distinctive-feature markers: circles you draw on a photo
CREATE TABLE feature_marker (
  id INTEGER PRIMARY KEY,
  person_id INTEGER NOT NULL REFERENCES person(id) ON DELETE CASCADE,
  photo_id  INTEGER REFERENCES photo(id) ON DELETE CASCADE,
  x REAL NOT NULL, y REAL NOT NULL,     -- normalised 0..1
  label TEXT, created_at TEXT NOT NULL
) STRICT;

-- ── photos (images live HERE, as BLOBs — constraint C2) ───────────────────
CREATE TABLE photo (
  id         INTEGER PRIMARY KEY,
  person_id  INTEGER NOT NULL REFERENCES person(id) ON DELETE CASCADE,
  is_primary INTEGER NOT NULL DEFAULT 0,
  mime       TEXT NOT NULL,
  width      INTEGER NOT NULL, height INTEGER NOT NULL,
  bytes      INTEGER NOT NULL,
  sha256     TEXT NOT NULL,             -- exact-duplicate detection
  phash      TEXT,                      -- perceptual hash → "these look alike"
  data       BLOB NOT NULL,             -- downscaled, max 1024 px long edge
  thumb      BLOB NOT NULL,             -- 256 px, quality 80 (grid/search/every list)
  crop       TEXT,                      -- json {"x":..,"y":..,"w":..,"h":..}
  source     TEXT,                      -- filename/note for provenance
  created_at TEXT NOT NULL
) STRICT;
CREATE UNIQUE INDEX photo_sha   ON photo(sha256);
CREATE INDEX        photo_person ON photo(person_id, is_primary DESC);

-- ── groups (overlapping, manual or rule-based) + tags ────────────────────
CREATE TABLE grp (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL CHECK (kind IN ('manual','rule')),
  rule TEXT,                            -- json filter for kind='rule'
  is_default INTEGER NOT NULL DEFAULT 0,
  color TEXT, created_at TEXT NOT NULL
) STRICT;
CREATE TABLE grp_member (
  grp_id INTEGER NOT NULL REFERENCES grp(id) ON DELETE CASCADE,
  person_id INTEGER NOT NULL REFERENCES person(id) ON DELETE CASCADE,
  PRIMARY KEY (grp_id, person_id)
) STRICT, WITHOUT ROWID;
CREATE TABLE tag (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE) STRICT;
CREATE TABLE person_tag (
  person_id INTEGER NOT NULL REFERENCES person(id) ON DELETE CASCADE,
  tag_id    INTEGER NOT NULL REFERENCES tag(id) ON DELETE CASCADE,
  PRIMARY KEY (person_id, tag_id)
) STRICT, WITHOUT ROWID;

-- ── one trainable card per (person, direction) ─────────────────────────────
CREATE TABLE card (
  id INTEGER PRIMARY KEY,
  person_id INTEGER NOT NULL REFERENCES person(id) ON DELETE CASCADE,
  direction TEXT NOT NULL CHECK (direction IN ('face_to_name','name_to_face')),
  fsrs      TEXT NOT NULL,              -- json.dumps(Card.to_dict(), default=str)
  due       TEXT NOT NULL,              -- denormalised for indexing (kept in sync)
  stability REAL, difficulty REAL, state INTEGER,
  reps INTEGER NOT NULL DEFAULT 0,
  lapses INTEGER NOT NULL DEFAULT 0,
  suspended INTEGER NOT NULL DEFAULT 0,
  UNIQUE (person_id, direction)
) STRICT;
CREATE INDEX card_due_idx ON card(due) WHERE suspended = 0;

-- ── append-only review log: drives stats, confusion pairs, FSRS optimisation ─
CREATE TABLE session (
  id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, ended_at TEXT,
  grp_ids TEXT, modes TEXT,             -- json arrays
  target_kind TEXT, target_value INTEGER, new_budget INTEGER,
  planned_items INTEGER, answered INTEGER NOT NULL DEFAULT 0,
  correct INTEGER NOT NULL DEFAULT 0
) STRICT;

CREATE TABLE session_item (                -- the server-side queue → resumable sessions
  id INTEGER PRIMARY KEY,
  session_id INTEGER NOT NULL REFERENCES session(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL, card_id INTEGER REFERENCES card(id), new_person_id INTEGER,
  mode TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending','answered','requeued','skipped')),
  requeues INTEGER NOT NULL DEFAULT 0,
  UNIQUE (session_id, seq)
) STRICT;

CREATE TABLE review (
  id INTEGER PRIMARY KEY,
  card_id    INTEGER NOT NULL REFERENCES card(id) ON DELETE CASCADE,
  session_id INTEGER REFERENCES session(id) ON DELETE SET NULL,
  reviewed_at TEXT NOT NULL,
  rating     INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 4),
  derived    INTEGER NOT NULL DEFAULT 1,  -- 1 = auto-rated, 0 = you overrode it
  mode       TEXT NOT NULL,
  correct    INTEGER NOT NULL,
  hint_level INTEGER NOT NULL DEFAULT 0,
  latency_ms INTEGER,
  chosen_person_id INTEGER,               -- the distractor you tapped → confusion matrix
  confidence INTEGER,                     -- optional 1..3 (calibration stats)
  elapsed_days REAL, scheduled_days REAL, duration_ms INTEGER
) STRICT;
CREATE INDEX review_card_time ON review(card_id, reviewed_at);
CREATE INDEX review_confusion ON review(card_id, chosen_person_id) WHERE correct = 0;

CREATE TABLE setting (key TEXT PRIMARY KEY, value TEXT NOT NULL) STRICT;

-- ── full-text search over names/notes (FTS5 is available in this image) ────
CREATE VIRTUAL TABLE person_fts USING fts5(
  display_name, nickname, mnemonic, where_met, notes,
  content='person', content_rowid='id', tokenize='unicode61 remove_diacritics 2'
);
-- The indexed display_name is the *resolved* name (override, else first name) with the family
-- name appended, written by the triggers — not the raw column. Migration 0004 repopulated the
-- index explicitly rather than with FTS5's 'rebuild', which would have re-read raw columns.
-- + three triggers (INSERT/UPDATE/DELETE) keeping person_fts in sync
```

**Deliberate choices worth flagging:**

* **Two cards per person** (`face_to_name`, `name_to_face`). They are different skills with
  different difficulty, and the scheduler should not average them. `name_to_face` is
  secondary: it is only trained when you opt into it or when two people look alike.
* **`due` is denormalised** onto `card` so "what is due in this group" is one indexed query
  rather than a JSON scan. One writer (the scheduler) keeps it in sync; a test asserts they
  never diverge.
* **`chosen_person_id` on `review`** is the single most valuable column in the schema — it
  is what makes the confusion-pair feature possible.
* **No ORM.** `sqlite3` + dataclasses. For this size, an ORM is a translation layer with no
  buyer.
* **Photos as BLOBs** are justified by sqlite.org's own benchmark; at 500 people × 3 photos
  × (1024 px ≈ 180 KB + 256 px thumb ≈ 20 KB) the database is ~300 MB. Healthy.

---

## 7. Image import pipeline

```
upload/camera/paste
   ├─ reject if > 25 MB or not JPEG/PNG/WebP/HEIC*        (*HEIC: accept only if pillow-heif added)
   ├─ ImageOps.exif_transpose()          # honour camera orientation
   ├─ strip ALL EXIF/GPS/ICC → re-encode # privacy: no GPS, no device serial
   ├─ downscale to max 1024 px long edge, JPEG q85 (or WebP q80)
   ├─ thumbnail 256 px long edge, q80
   ├─ sha256 (dedupe) + phash (similarity)
   └─ INSERT into photo (single transaction, BLOB written once)
```

* **Why downscale at import:** the app's largest display is a tablet in portrait (~1200 px).
  Storing 12 MP originals triples the file size for zero visible benefit. If you want
  originals kept, that is a one-line setting (Q10: settled — downscaled copies only).
* **`phash` paying off twice:** (a) it powers "these faces look alike", which is how the app
  finds *plausible distractors* even before you have any history; (b) it warns you when you
  file the same person twice.
* **Cropping:** a cropper in the browser (canvas) writes `crop` JSON; the server derives the
  displayed image on the fly and caches the result as a new `data`/`thumb`. Manual, cheap,
  no face-detection dependency (Q4: settled — manual crop only).
* **Serving:** `GET /photos/{id}.webp?size=thumb|full` with `ETag`/`Cache-Control: immutable`
  and an `If-None-Match` 304 path. Lists always use `thumb`; the trainer uses `full`.

---

## 8. Scheduler integration

**One card state per (person, direction), owned by py-fsrs**, persisted as the JSON dict
described in §2.

### 8.1 Answer → rating (the derivation you asked for)

The four FSRS ratings are derived from *observable* behaviour, so you rarely have to rate
anything explicitly. Exactly one tap of thought, never a form.

| Your behaviour | Rating |
|---|---|
| Wrong answer, "I have no idea", or full hint ladder used | **Again (1)** — also re-queued in this session |
| Correct but used a hint (level 1–2), or slow (> 1.5 × your median for this person) | **Hard (2)** |
| Correct, no hint, normal speed | **Good (3)** |
| Correct, no hint, fast (< 0.6 × median) *and* the card already had high stability | **Easy (4)** |

* Each answer row records `derived=1`; if you override the rating by tapping it, `derived=0`.
* `Rating.Again` increments `card.lapses`; `lapses >= 4` marks a **leech** → the app stops
  the plain re-ask and offers the encoding/edit flow instead (mnemonic, feature marker,
  extra photo, then reset the card to Learning).
* "I have no idea" is logged distinctly from a wrong answer: it means the item was never
  encoded, so the app should show the *study card* rather than test it again immediately.
* **Per-item, not per-session, difficulty.** FSRS's own `difficulty` is used as the first
  guess for a new card; the app's role is to pick *distractors* and *mode*, not to
  second-guess the memory model.

### 8.2 Within-session loop (the spaced-retrieval / successive-relearning part)

```
answer wrong  → show correct name + photo + mnemonic immediately
              → re-queue the item after ~5 other items
              → on next failure, re-queue after ~10; cap at 3 requeues per session
answer right  → if correct_count_for_this_card_in_session >= criterion (default 1):
                   retire it for the session
                else: re-queue after ~8 other items
```
This mirrors the clinical protocol: on error, correct immediately and re-ask at a shorter
interval, never letting the learner sit in failure; on success, extend. The *session-level*
criterion (default 1 correct, configurable 1–3) is the successive-relearning knob: three
correct recalls in **spaced sessions** beat three in one sitting, which is why the app
prefers to end a session and come back tomorrow rather than grind now.

### 8.3 Intervals for knowledge you want to keep for years

`desired_retention` defaults to **0.90** (FSRS's standard; 0.85 → fewer reviews, more
misses). Because names are knowledge with a *very* long intended retention interval, FSRS
will naturally stretch intervals to weeks and months. That matches the optimal-gap finding
(best first gap ≈ 10–20 % of the retention interval). I do **not** hard-code an expanding
schedule: the meta-analytic evidence says expansion is not reliably better than uniform
spacing (*g* = 0.034) except when an item is tested many times — so expansion lives only in
the in-session loop above, where it demonstrably helps.

---

## 9. The item-selection engine — the heart of the app

FSRS says *when* a card is due. It does not say *which of the 60 due cards* I should see in
the next 10 minutes, nor how to mix them. That is this engine. It lives in `app/selection.py`
(**built, M3** — `trainer.build_session` calls it and nothing else orders the queue), and it is
the part of the app with the most comments per line, because every number in it is a claim
about memory that has to be defensible later.

### 9.1 Priority score

```python
retrievability = scheduler.get_card_retrievability(card, now)   # 1.0 = fresh, 0.0 = forgotten

score = (
    0.45 * (1 - retrievability)        # what you are about to forget
  + 0.20 * confusion                   # min(1, mix-ups / 2), counted on both sides of the pair
  + 0.15 * difficulty                  # (card.difficulty - 1) / 9
  + 0.15 * overdue_ratio               # min(1, days_late / max(0.5, stability))
  + 0.05 * lapses                      # min(1, lapses / 3)
)
```

Two deliberate departures from the sketch this section started life as, and the reasoning, so
that neither looks like an accident:

* **`context_diversity` is gone; its weight went to retrievability (0.40 → 0.45) and confusion
  (0.15 → 0.20).** "Long unseen" is not really a separate signal: a card nobody has seen in
  months either has a low retrievability — which the first term already reports — or it does
  not, in which case asking it sooner is precisely the exploration mistake the floor in §9.1.3
  exists to prevent. Confusion, by contrast, is the thing this app was built for, so it takes
  the larger share.
* **"Recent failure decay" is a saturating lapse count, not a time-decayed one.** FSRS already
  drops a card's stability on a lapse, so how recently you failed is already priced into
  `retrievability`; a second exponential over the same event would double-count it. The term
  stays deliberately small (0.05) — a tie-breaker, never a driver.

`overdue_ratio` is relative to the card's own strength (`stability`) rather than to a fixed
number of days: three days late on a five-day interval is a different event from three days late
on a three-hundred-day one.

A card with **no review behind it** has no retrievability at all — `get_card_retrievability`
answers `None` (or refuses) for it, and the wrapper reports `0.0`, "fully at risk", instead of
letting that state crash the queue. That is the honest default for a card the model can say
nothing about, and it keeps the first run after an import sane.

Then, three *constraints* that matter more than the weights:

1. **New-item budget** — at most `new_budget` (default 3) never-seen people per session; each
   new person is preceded by its **study card** (first exposure must not be a test).
2. **Interleaving — two passes, in this order.** `selection.spread_apart` keeps faces whose
   perceptual hashes are within `photos.similar`'s Hamming threshold (14) out of adjacent slots;
   the chunk interleave then keeps the same *person* out of the last `interleave_window` items.
   The second pass preserves the first's order, so scoring keeps its say. One threshold serves
   both the distractor engine and the queue, so "looks like them" means one thing in the app.
3. **Maintenance floor** — `maintenance_floor` (a setting; default 10 %) of the slots go to cards
   the model believes are *known*, drawn at random from those the score would **not** have taken.
   Both halves are load-bearing. A "maintenance" card the score wanted anyway is not maintenance
   at all, and — the subtle one — the session budget is handed *into* `selection.pick`, so the
   floor decides **membership** and not merely position; when the pick was `len(pool)` and the
   plan cap trimmed afterwards, the floor could be trimmed off the end entirely. Without it the
   engine stops asking what it thinks you know, never discovers that it is wrong, and rots
   silently. (In ML terms: keep an exploration term; do not let the model's own beliefs starve
   the evidence.)

### 9.2 Weighted sampling without replacement

```python
def _weighted_sample(pool, count, rng):            # app/selection.py, the shipped shape
    remaining, drawn = list(pool), []
    while remaining and len(drawn) < count:
        weights = [(card.score + REWARD_FLOOR) ** SHARPNESS for card in remaining]
        choice = rng.choices(remaining, weights=weights, k=1)[0]
        remaining.remove(choice)
        drawn.append(choice)
    return drawn
```
`rng` is injected with a seed → sessions are reproducible in tests. `SHARPNESS` (3.0) sets how
close to argmax the sampler runs; `REWARD_FLOOR` (0.04) keeps even a zero-score card reachable.
Sampling rather than taking the maximum every time is the entire point: **argmax makes the order
deterministic, and a learner who notices starts learning the order** instead of the faces.

The score weights themselves are *constants in `selection.py`*, not settings. An untuned knob is
not configuration, and there is no evidence yet to tune them with; `maintenance_floor` is the
exception because the schema already anticipated it (§0003). When there is evidence — a session
log worth regressing against — they move into `setting` and get a UI.

### 9.3 Mode selection per item

The ladder as shipped (`trainer.choose_mode` + `ladder.choose`) may only ever choose from the modes
the run was allowed to use — and there are **two ladders**, one per card direction, because putting
a name to a face and putting a face to a name are different skills:

```
never asked in this direction, or a lapse on record  → the gentlest ticked mode of that direction
last answer in this direction was a miss             → one rung down
last answer in this direction was right              → one rung up
nothing on record for this card yet                  → the gentlest ticked mode
```

A "rung" is a step in *demand* within the modes ticked for that direction (`ladder.tiers_for`), so
a face climbs `mc4 → confusion → recall_self` on the face→name axis and `reverse → listen` on the
name→face axis. A rung that cannot be asked right now is not a rung: the confusion drill leaves a
face's ladder until that face has a mix-up on record, so a settled face steps from multiple choice
to free recall only when there is genuinely nothing between them.

Promotion needs **evidence, not a count**. The first version promoted on `reps >= 3 and lapses ==
0`; that is a counter, not a memory, and a card whose numbers merely looked settled could be moved
up into a mode it had never once been asked in. Climbing now requires having been asked the rung
below and answered it correctly — which is also what makes demotion legible: a miss drops the face
one rung, and the very next question shows it.

Two rules with teeth:

* **"One rung up" must stay inside what you ticked.** The ladder never invents a mode: tick multiple
  choice alone and every question is multiple choice, however well you know the face. That is what
  makes the builder's default — multiple choice **and** free recall — a real recommendation rather
  than decoration: ticking both is what lets a face climb from recognition toward production.
* **Never let the ladder be invisible.** A learner who ticked one mode and was then asked in another
  cannot tell that the app obeyed; so the run screen names the modes in play ("This run can ask: …")
  and the builder's hint states the rule in one sentence. Both exist because their absence produced
  a bug report that looked exactly like a mode-selection bug and was not one.

**Direction decides the card, so it decides the schedule.** A reverse or listening question is
graded into that person's `name_to_face` card, never the `face_to_name` one: averaging the two
skills into one FSRS schedule would let a face you recognise but cannot name read as "known". The
second card is created lazily — on the first run that ticks a backwards mode, and only for people
who already have a card the right way round, so nobody is asked to name a face backwards before
they have met it. A card created in that same request counts as due in it; without that nudge the
new card's `due` lands microseconds after the session's own `now` and the first reverse run finds
nothing to ask.

This is "asking more of what you get wrong, less of what you always get right" — for the *choice of
cards* it is a probability distribution, not a rule that flips between extremes: nothing is ever
removed from the rotation; its frequency drops as your stability rises. The `study` card is
deliberately **not** a recommended default: a never-seen face is introduced with one regardless of
what you tick, so ticking it means "also show me faces I have already met" — a different wish.

---

## 10. Exercise-mode plugin architecture

Every mode is a small class registering itself; the session builder's mode checklist is
rendered **from the registry**, so "modes are optional per training run" is structural rather
than something I remember to code each time. Adding a mode = adding one module.

```python
@dataclass(frozen=True)
class Mode:
    key: str                 # 'mc4', 'match3', 'match6', 'recall_self', 'listen', 'reverse', 'study', 'confusion'
    label: str               # i18n key
    direction: str           # 'face_to_name' | 'name_to_face' | 'none'
    demand: int              # 1..5 — used by adaptive difficulty and promotion
    min_people: int          # 4 (mc4), 3 (match3), 1 (recall_self/study)
    input: str               # 'tap' | 'drag' | 'self' | 'none'
    needs_distractors: int   # 0 | 3 | …

    def build(self, target, pool, rng) -> ItemPayload: ...     # → JSON for the UI
    def grade(self, payload, answer) -> Outcome: ...           # → correct, chosen_person_id, hint_level
```

* The **server** builds payloads and grades; the **client** only renders and collects input.
  No answer logic on the client → no client/server drift, and grading stays testable.
* `Outcome` carries `correct`, `chosen_person_id` (feeds the confusion matrix), `hint_level`,
  `latency_ms` — everything §8.1 needs for the auto-rating.
* `demand` is what makes mode promotion possible: the engine can move a person up
  (`mc4` → `match3` → `recall_self`) as they stabilise, and down when they fail repeatedly.
* Modes needing more people than the pool has are simply not offered (that is why the
  first-run "fewer than 4 people → free recall" detail in doc 1 §11 falls out for free).

---

## 11. HTTP API

HTML routes (Jinja) for pages; JSON routes for the trainer loop. All under `/api`, all
JSON, all with a session cookie when auth is on.

| Method & path | Purpose |
|---|---|
| `GET /` | Dashboard: what is due, quick start, last session |
| `GET /people`, `GET /people/new`, `POST /people` | List/search; add form; create |
| `GET /people/{id}`, `PATCH /people/{id}`, `DELETE /people/{id}` | Detail (stats, photos, history); edit; archive/delete |
| `POST /people/{id}/photos` (multipart) | Upload one or many photos |
| `PATCH /photos/{id}` / `DELETE /photos/{id}` | Set primary, re-crop, delete |
| `GET /photos/{id}.webp?size=thumb\|full` | Image bytes (ETag, 304, immutable) |
| `GET/POST/PATCH/DELETE /groups`, `POST /groups/{id}/members` | Group CRUD; add/remove people |
| `GET /train` | Session builder (renders the mode checklist from the registry) |
| `POST /api/sessions` | Create session → returns `session_id` + **first item** |
| `POST /api/sessions/{id}/answer` | Submit an answer → verdict, correct name/photo, derived rating, **next item** |
| `POST /api/sessions/{id}/hint` | Reveal next hint level (returns the hint text only) |
| `POST /api/sessions/{id}/rate` | Override the derived rating |
| `POST /api/sessions/{id}/people` | Quick-add a person mid-session without losing the queue |
| `POST /api/sessions/{id}/end` | Finish → summary (counts, misses, next due dates) |
| `GET /api/sessions/{id}` | Full session state — makes **resume after a closed tab** free |
| `GET /stats`, `GET /people/{id}/stats` | Dashboard aggregates; per-person stats + confusion pairs |
| `GET /sheet?group=X` | Printable cheat sheet (print-CSS HTML) |
| `GET /api/backup` | `VACUUM INTO` a snapshot, stream it as a download |
| `POST /api/import` | Import a `.sqlite` (replace-with-confirmation) |
| `GET /api/health` | Liveness for the container healthcheck |

**Trainer response shape** (the whole loop, deliberately one round-trip per answer):

```json
{
  "session": {"id": 12, "answered": 7, "correct": 5, "planned": 24, "remaining": 17},
  "verdict": {"correct": true, "expected_person_id": 42, "chosen_person_id": null,
              "rating": 3, "derived": true, "correct_name": "Anna Brunner",
              "mnemonic": "Anna-logue watch", "hint_level": 0},
  "next": {
    "item_id": 88, "mode": "mc4", "direction": "face_to_name",
    "photo_url": "/photos/77.webp?size=full",
    "options": [{"person_id": 42, "label": "Anna Brunner"},
                {"person_id": 51, "label": "Andrea Weber"},
                {"person_id": 63, "label": "Beatrice Widmer"},
                {"person_id": 19, "label": "Claudia Meier"}],
    "demo": {"flash_on_error": true}
  }
}
```

---

## 12. Frontend architecture

* **Pages are server-rendered Jinja templates.** The people/cabinet/groups/stats screens use
  **HTMX** for partial updates (`hx-post`, `hx-swap`) — no JSON glue, no client-side router.
* **The trainer is the one stateful screen:** `trainer.js` — a ~200-line explicit state
  machine, `loading → prompting → hinting → revealing → next`, rendering whatever payload
  the server sends. Drag & drop uses **Pointer Events**, not HTML5 DnD (works identically
  with mouse, touch and pen, and is testable). Every drag interaction has a tap-tap fallback.
* **No build step, no CDN, offline-capable:** `htmx.min.js` and the fonts are vendored into
  `static/`. Optional PWA: a `manifest.webmanifest` + a tiny service worker caching the shell
  and `thumb` images; the trainer itself requires the server (that is fine — it runs on your
  own host).
* **CSS:** one `kith.css`, custom properties for theme (dark default, light alternate),
  `clamp()` typography so photos dominate on phones, `prefers-reduced-motion` honoured.
* **Accessibility as a design constraint:** every photo has `alt` = the person's name *only
  after* a question is answered (before that, `alt=""` — otherwise the answer is in the DOM
  for screen readers *and* in the HTML for the curious). Full keyboard operation, focus
  rings, no colour-only feedback, hit targets ≥ 44 px.
* **The answer must not be in the payload before it is answered** — `next` contains only what
  is needed; the correct name arrives with the verdict. (Cheap anti-cheat, but mainly it
  keeps the interaction honest.)
* **i18n:** all strings from `locales/en.json`; no string literals in templates. (German is a
  drop-in later — a second file, not a refactor.)

---

## 13. Session lifecycle

```
client                server                              sqlite
  │  POST /api/sessions │                                   │
  │────────────────────▶│ groups → pool → due cards         │
  │                     │ score (FSRS retrievability)  ─────▶│ SELECT card …
  │                     │ build_queue (interleave, new budget)
  │                     │ persist session + session_item ───▶│ INSERT …
  │◀────────────────────│ first payload                     │
  │ render prompt       │                                   │
  │  POST …/answer      │                                   │
  │────────────────────▶│ grade → Outcome                ───▶│ (no write yet)
  │                     │ derive rating (hint/latency)      │
  │                     │ fsrs.review_card(card, rating) ──▶│ UPDATE card (fsrs,due)
  │                     │ INSERT review (+chosen_person_id) │
  │                     │ requeue or retire session_item ──▶│ UPDATE session_item
  │                     │ pick next (skip recent people)    │
  │◀────────────────────│ verdict + next payload            │
  │  … loop …           │                                   │
  │  POST …/end         │ summary + next-due forecast ─────▶│ UPDATE session.ended_at
```
Everything authoritative lives server-side in the database, so a session survives a browser
crash, a phone lock, or a container restart — `GET /api/sessions/{id}` resumes it.

---

## 14. Security & privacy

* **Never leaves your machine.** No outbound calls; no analytics; no fonts from a CDN; no
  image ever uploaded anywhere. This is a hard design rule, and it is also why the "AI face
  recognition" idea is out of scope.
* **Auth:** off by default when bound to loopback only. When published behind Caddy, an
  app-owned session cookie login (stdlib `scrypt` hash + `secrets.compare_digest`), `HttpOnly`,
  `SameSite=Lax`, `Secure` when served over HTTPS, 30-day sliding expiry. Your preference for
  the app's own gate over Caddy `basic_auth` is respected — Caddy gets a bare `reverse_proxy`.
  As built (M1):
  * **No user table.** One password hash in the environment; the cookie is a signed token
    (`HMAC-SHA256` over its own payload, stdlib only). There is nothing to enumerate.
  * **Constant-time cost when unconfigured.** If `KITH_AUTH=on` but no hash is set, the
    verifier still runs a dummy scrypt against a decoy hash, so "no password configured"
    and "wrong password" take the same time. A missing hash fails closed with a startup
    warning, never by waving logins through.
  * **What stays public:** `/login`, `/logout`, `/static/*`, `/favicon.ico`, and
    `/api/health`. Every other path — including `/api/*` — redirects to the login page (403
    for `/api/*`, so an XHR never receives an HTML page and mistake it for data).
  * `KITH_SESSION_SECRET` signs the cookie. Unset means a per-process random secret and a
    startup warning: logins then die on restart, which is survivable but worth avoiding.
  * `set-password.sh` exists because the hash contains `$` characters: written by hand into
    the env file with `sed` inside double quotes, `$16384` silently expands to nothing and
    you get a value that only fails much later, as "wrong password".
* **Quoted values survive both env-file parsers.** Quadlet's `EnvironmentFile=` reaches the
  container through podman's `--env-file`, which keeps surrounding quotes, while systemd's
  own `EnvironmentFile=` strips them. The credential is therefore quoted in the file (it
  contains `$`) and `config._unquote` drops one layer of quotes, whichever parser ran.
  Without it the hash compares as `'scrypt$…'` and the password never matches — with no
  error anywhere, which is the worst kind of bug.
* **Bound to `127.0.0.1` on the host**, never `0.0.0.0`, so the container is not on your LAN
  unless you deliberately expose it.
* **CSRF:** same-site cookie + a required `X-Kith-CSRF` header on state-changing JSON routes.
* **Photos of real people:** EXIF/GPS stripped on import; the DB file lives in a `0700`
  directory; no thumbnails ever rendered into logs or error pages.
* **Destructive actions** (delete person, import/replace DB) require typed confirmation and
  offer the backup download first.

---

## 15. Testing strategy

| Level | What is covered |
|---|---|
| **Unit** | Rating derivation table (behaviour → 1..4, every row of §8.1). Selection scoring + interleaving constraint (no target within *W* items; no adjacent confusable pair). Weighted sampler determinism with a seeded RNG. Image pipeline: dimensions, orientation, EXIF actually gone, thumbnail size, dedupe by sha256. |
| **Scheduler contract** | `card → JSON → card` round-trip is byte-identical, **including the §2 pitfall**: the test asserts `Card.from_dict(json.loads(json.dumps(d, default=str)))` works and that passing real datetimes raises. Also asserts the denormalised `card.due` column always equals `card.fsrs["due"]`. |
| **API** | `TestClient` against a temp DB: create person → upload photo → build session → answer → resume → end. Auth on/off paths, 401 without cookie, CSRF rejection. |
| **E2E (Playwright)** | The drag modes really work: `match3` with pointer events, tap-tap fallback, keyboard-only run of a 5-item session, and the hint ladder. |
| **Data integrity** | FK cascade deletes leave no orphans; `person_fts` matches after edit/delete (trigger correctness). |
| **Perf smoke** | Seed 500 people × 3 photos (~300 MB), assert session creation < 200 ms and a `thumb` fetch < 20 ms. |
| **Seed data** | `tools/seed_demo.py` generates synthetic faces (procedural avatars, no real people) so tests and demos never contain anyone's photo. |

---

## 16. Deployment runbook (Podman + Quadlet + Caddy)

```bash
# 0) ship the source to the host as the tenant user (never plain sudo — see pitfalls)
tar czf kith-src.tgz . && scp kith-src.tgz <host>:/tmp/
ssh <host> 'cd /tmp && sudo -u barn mkdir -p /home/barn/kith \
  && sudo -u barn tar -xzf /tmp/kith-src.tgz -C /home/barn/kith'

# 1) build the image AS THE TENANT USER: rootless image storage is per-user, so an
#    image built as root is invisible to barn's Quadlet unit. --format docker is not
#    optional — without it Podman 5 builds OCI format and drops the HEALTHCHECK.
ssh <host> 'cd /tmp && sudo -u barn XDG_RUNTIME_DIR=/run/user/1001 \
  podman build --format docker -t localhost/kith:0.1 \
               -f /home/barn/kith/Containerfile /home/barn/kith'

# 2) data dir — owned by barn, OUTSIDE /home (see pitfalls), relabelled on mount
sudo install -d -o barn -g barn -m 0700 /srv/kith/data

# 3) install the Quadlet unit + its env, then start it as a user service
mkdir -p ~/.config/containers/systemd ~/.config/kith
cp kith.container ~/.config/containers/systemd/
install -m 600 kith.env.example ~/.config/kith/kith.env   # then edit: KITH_AUTH=on + hash
systemctl --user daemon-reload && systemctl --user start kith
loginctl enable-linger barn          # survive logout / start at boot

# 4) Caddy (no auth block — the app has its own login)
#    /etc/caddy/Caddyfile.d/kith.caddyfile:  kith.barn.opq.ch { reverse_proxy 127.0.0.1:8080 }
sudo caddy validate --config /etc/caddy/Caddyfile && sudo systemctl reload caddy

# 5) verify — inside the container, then the host, then the public name
podman exec kith id && podman exec kith test -w /data && echo "data writable"
curl -sS http://127.0.0.1:8080/api/health             # {"ok":true,"schema":1}
curl -sS -4 https://kith.barn.opq.ch/api/health       # from the HOST, never inside a container
```

**Deployment pitfalls to expect (from the `barn-host-webapp-deploy` skill and prior host work):**

* Host firewall must permanently allow `http`/`https`:
  `sudo firewall-cmd --permanent --add-service=http --add-service=https && sudo firewall-cmd --reload`.
* Do **not** diagnose connectivity from inside a container: `*.opq.ch` maps to loopback there,
  so failure is meaningless (family IPv6 hairpin).
* **SELinux `:Z` + ownership — the Fedora-specific one.** The `:Z` suffix is *required* on a
  bind mount, or the container gets `Permission denied` on the path; but relabelling fails
  with **EPERM if the directory contains root-owned files**. Create the data dir with
  `sudo install -d -o barn -g barn` (or as `sudo -u barn`) — never plain `sudo mkdir`
  followed by a chown, because the root-owned entries in between are exactly what breaks the
  relabel. This is a recorded lesson from the Hermes container work, not a hypothetical:
  files created via plain `sudo` under the barn home became root-owned, the `:Z` relabel hit
  EPERM, and the service restart then failed. Symptom to recognise:
  `Error relabeling … operation not permitted`.
* **Keep the data dir out of `/home/barn`** — Fedora's default policy treats home content as
  user home data, and relabelling inside a home directory is the classic source of the EPERM
  above. `/srv/kith` sidesteps the whole class of problem; this is the standard
  recommendation, not a workaround.
* **UID mapping:** rootless `UserNS=keep-id:uid=10001,gid=10001` has to be verified
  (`podman exec kith id` should print uid 10001, and `podman exec kith test -w /data` must
  pass). A mismatch surfaces later as `attempt to write a readonly database`.
* Keep the data out of the build context: `.containerignore` contains `data/`, so a build can
  never bake your photos into an image layer.
* **Rootless podman refuses to start from a directory it cannot read.** It resolves the
  working directory on startup, so any `sudo -u barn <podman …>` that inherits
  `/home/fedora` (mode 0700) dies with `cannot chdir to /home/fedora: Permission denied`.
  Use `sudo -iu barn` (login shell, starts in barn's home), or prefix `cd /tmp &&`.
  This is the single most confusing failure in this runbook, because with a credential
  script the prompts come first and the error reads like a rejected password.
* **An image ENTRYPOINT swallows the command you pass to `podman run`.** `podman run
  <image> python -m app.cli show` boots the server instead (and hangs until the call times
  out, leaving a container behind — `podman ps -a`, then `podman rm -f`). From a *running*
  container use `podman exec -it kith python -m app.cli …`, which bypasses the entrypoint;
  for a one-off from the image name it explicitly:
  `podman run --rm --entrypoint python --env-file … localhost/kith:0.2.0 -m app.cli show`.
* **`/api/health` stays reachable without a session** (so a monitor can tell "up" from "down"),
  but an unauthenticated caller gets a trimmed payload: liveness, schema version, readiness.
  No paths, no versions, no counts. Keep it trimmed when adding fields.
* **`HEAD /login` answers 405.** The route is registered with an explicit method list, so
  Starlette does not add HEAD automatically. Harmless for browsers (they use GET), but worth
  tidying next time that route is touched.
* **Linger:** without `loginctl enable-linger barn` the unit dies at logout (and does not come
  back after a reboot).
* **Updates:** `podman build -t localhost/kith:0.2 …`, bump `Image=`, then
  `systemctl --user daemon-reload && systemctl --user restart kith`. (`AutoUpdate=` only
  applies to registry images, so a locally-built image is updated by rebuilding — which a
  tiny `kith-build` unit can automate if we ever want it.)

---

### 16.1 First exposure: password, then Caddy (M1)

Order matters. The Caddy site must not exist while `KITH_AUTH=off`, or the app sits on the
internet with no gate for as long as it takes you to notice.

```bash
# 1) set the password. Prompts twice, never echoes it, hashes inside the container, backs
#    up the env file, then proves the gate is on (GET / → 303, GET /login → 200).
#    Use `sudo -iu barn` (login shell), not `sudo -u barn`: the non-login form inherits
#    the caller's cwd (/home/fedora, 0700), which barn cannot enter, and rootless podman
#    aborts with "cannot chdir to …: Permission denied" *after* the password prompts —
#    indistinguishable from a rejected password. The script also `cd`s out of the way, so
#    both forms work now, but `-i` is the one to keep.
ssh -t fedora@pantheon.opq.ch \
  'sudo -iu barn XDG_RUNTIME_DIR=/run/user/1001 bash /home/barn/kith/scripts/set-password.sh'

# 2) publish it: one file, a bare reverse_proxy — the gate is the app's own, so Caddy
#    gets no basic_auth (per the barn-host-webapp-deploy convention)
sudo tee /etc/caddy/Caddyfile.d/kith.barn.opq.ch.caddyfile >/dev/null <<'EOF'
kith.barn.opq.ch {
	reverse_proxy 127.0.0.1:8080
}
EOF

# 3) reload and confirm; if the reload is refused, the reason is in the journal
sudo systemctl reload caddy && sudo journalctl -u caddy -n 20 --no-pager
```

Then, from anywhere: `curl -sI https://kith.barn.opq.ch/` must answer **303** with a
`Location:` of `/login`, and `https://kith.barn.opq.ch/login` must render the form. Caddy
obtains the certificate on the first request, so `kith.barn.opq.ch` has to resolve to the
host before you try (otherwise you get a TLS error that looks like a Kith problem and
isn't).

**Updating a running instance later:** ship the source (step 0 above), build
`podman build --format docker -t localhost/kith:<version>` as the tenant, bump `Image=` in
the unit, then `daemon-reload && restart`. Migrations apply themselves on start, and the
`ready — … applied=…` log line tells you what ran.

---

## 17. Roadmap

| Milestone | Deliverable | Acceptance test |
|---|---|---|
| **M0** ✅ **done — verified on the Barn host 2026-10-01** | Container skeleton: Containerfile, Quadlet unit, healthcheck, migrations runner, one page. | `podman build` + `systemctl --user start kith` → health green, `podman exec kith test -w /data` passes, healthcheck reaches `healthy`, and a restart logs `applied=none (already up to date)`. **All confirmed on the live host.** |
| **M1** ✅ **done — built, deployed, and user-confirmed 2026-10-02** | Cabinet: people CRUD, photo upload + pipeline, groups (manual), FTS search — plus the login gate (§14) and `scripts/set-password.sh`, because going behind Caddy needs the gate. | Add a person with 2 photos from the phone; find them by typing half a name. **Confirmed by the user 2026-10-02:** two people added from the phone (one with two photos), *"Works perfectly."* — including search that filters as you type. **Confirmed so far:** full suite green; image `localhost/kith:0.2.0` live as barn; health `schema:2 ok:true db_writable:true`; startup log `applied=0002_cabinet.sql` on the real database; `/ /cabinet /groups /people/new` → 200 and `/login` → 303 while the gate is off; a smoke run imported two EXIF-rotated 1600×1200 uploads as 768×1024 WebP (4 KB) plus 192×256 thumbs, the cabinet rendered the thumbnail, `?q=muller` matched *Müller*, and a group was created. **Since then:** the password is set and the gate is on (`/` → `303` to `/login?next=/`, `/login` → `200`, session cookie `Secure`), and the Caddy site kept in `deploy/` is live — `https://kith.barn.opq.ch/` serves over TLS 1.3 with a Let's Encrypt certificate (`CN=kith.barn.opq.ch`, valid to 30 Dec 2026) issued after a successful public ACME validation, which also proves the name is reachable from the internet. Two search bugs found in first real use (`0.2.1`): the filter form's empty `group=` field was typed as `int | None`, so every search from the UI answered 422, and the mid-word LIKE fallback rebuilt its query without the group/status filters, so searching inside a group listed the whole cabinet. Search now also **filters as you type**: ~60 lines of hand-written JavaScript swap the `#results` region with the one from the same URL (no JSON API, no second rendering path, aborted in-flight requests, `history.replaceState` so a refresh keeps the filters) — and the plain form still submits with JavaScript off. Verified in a real browser: in-place updates with no reload, mid-word fragments, group filtering that does not leak, and no sideways scroll at a 390 px viewport. **What remained was the part only you can do: open it on the phone and add a person with two photos** — done 2026-10-02. |
| **M2** ✅ **built and deployed 2026-10-02** | Trainer core: session builder, `study` + `mc4` + `recall_self`, verdicts, in-session requeue, rating derivation, summary. | Complete a 10-minute session; every prompt answerable with one tap. **Built:** migration `0003_scheduler.sql` (`card`/`session`/`session_item`/`review`, `STRICT`); modes as self-registering plugins whose `min_people` decides what a cabinet is big enough for; the queue held server-side so a closed tab resumes; a study card always precedes the first test of a new face, and the two are one indivisible chunk; a missed item returns ~5 places later, then ~10, capped at 3; *"no idea"* inserts a **study** card rather than re-testing; hints (first letter → syllable count → two letters → half → the name) cap the rating; the reveal is fetched from the server, so the name is **never in the page** while you are trying to produce it; a mode refuses an answer it cannot grade (409) instead of inventing a verdict; progress counts *items retired*, so "12 of 12" cannot appear after twenty taps. **Verified:** 78 tests green; a real browser walked both modes — reveal, hint chips, the miss showing the name and waiting for Continue, the correct answer flashing ✓ and moving on by itself, number keys choosing options, "Never seen them", the requeue badge, no sideways scroll at a 390 px viewport, and the summary page (accuracy, *Worth a second look*, *drill these again*, *When they come back*). Deployed as `localhost/kith:0.3.0` (schema 3) via the new `scripts/deploy.sh`, which checks its own work. |
| **M2.1** ✅ **built 2026-10-02** | The first round of trainer feedback: the first name *is* the asked name (`display_name` becomes an optional override, resolved in SQL rather than copied), the family name is shown smaller and grey *under* the answer, a **practise anyway** switch so a run can always be started, and group cards show the face instead of a letter. | Add someone with just a first name and be asked exactly that; see the last name appear small, never asked for; press *practise anyway* on a day when nothing is due and still get a run; open a group and see photos. **Built:** migration `0004_recall_name.sql` (backfills first names from one-word display names, reindexes the resolved name *plus* the family name), `people.RECALL_NAME` shared by every query, the switch through `build_session(include_all=…)`, thumbnails in `groups.members()`. Suite green (**86 tests**) with a regression for each of the four. |
| **M2.2** ✅ **built 2026-10-02** | Second feedback round: the family name sits *below* the first name rather than behind it, and the trainer's group dropdown names each group's size (`Padel crew (2)`) instead of `()`. | Open the trainer and read the group sizes; see the surname on its own line under the answer. **Built:** the trainer template asked for `member_count` while the query returns `members` (the only place in the app using the wrong name — every other template used the right one, which is why the cabinet's filter never showed the bug); `.name-family` is `display: block`. **87 tests** green, including a regression that fails on `Padel crew ()`. |
| **M3** ✅ **built and deployed 2026-10-02** | **Selection engine**: priority scoring (retrievability deficit, confusion history, difficulty, overdue relative to the card's own strength, lapses), weighted sampling without replacement, the 10 % exploration/maintenance floor aimed at confusables, interleaving that keeps phash-similar faces apart, and the honesty tests that keep auto-rating from running away. *(FSRS wiring and behaviour-based rating derivation landed early, with M2 — a card without a due date cannot be queued at all and a rating without a rule is a coin toss.)* | After 2 weeks of use, the app visibly asks weak people more often; due dates are sane. **Built as `app/selection.py`** (+ `trainer.build_session` handing it the session budget, so the floor governs *membership* and not just order) — **no migration: `SCHEMA_VERSION` stays 4**, because every feature is computed from columns that already exist (`due`, `stability`, `difficulty`, `lapses`, `review.chosen_person_id`, `photo.dhash`). Shipped as `localhost/kith:0.5.0`. **98 tests**, 12 of them new and named for the behaviour: a face you are about to forget outranks one you know, mixing two people up raises *both*, the pick never repeats or invents a card, the order is *sampled* (three identical cards must not always produce the same first item), the floor spends its slots on what the model thinks is known and reaches past what scoring alone would take, lookalikes are never adjacent but a queue of nothing *but* lookalikes still runs in full, and a card never reviewed is treated as fully at risk rather than crashing the queue. **On a seeded 26-face cabinet:** the queue no longer opens with the most overdue card but with the most *confused* one (two mix-ups logged → confusion term 1.00 → first and third slots), a 10-item session drawn from that cabinet reached three cards *below* its tenth-best score (a top-ten list cannot), and a planted lookalike pair — Hamming distance **0** — landed 7–13 slots apart in two independent real sessions, never adjacent. **In the live UI:** a forced run opened on exactly the face the engine scored first, and the tap was recorded (`correct=1`). |
| **M3.1** ✅ **built 2026-10-02** | A bug report that *looked* like mode selection and was not: the builder pre-ticked three modes (`study`, `mc4`, `recall_self`), so a run the learner believed was "multiple choice only" was allowed to promote settled faces to free recall — which it then did, exactly as designed, with no way to tell. | Tick multiple choice alone and be asked nothing else; be able to see which modes a run may use. **Built:** `study` is no longer pre-ticked (a never-seen face gets a study card whatever you tick, so ticking it means something else entirely); the ladder is stated in the builder's own hint in one sentence; the run screen names the modes in play; and a regression test ticks multiple choice alone over deliberately *settled* cards and fails if anything else is asked. **Evidence:** session 8 in the live database recorded `modes: ["study","mc4","recall_self"]` — the submitted set was the surprise, not the ladder — and a fresh demo run created with `modes: ["mc4"]` produced 26 multiple-choice items and nothing else. **101 tests** (3 new). Shipped as `localhost/kith:0.5.1`. |
| **M3.2** ✅ **built 2026-10-02** | The builder forgot your choices on every reload — including the reload a refusal shows you ("nothing is due"), so the mode you had just picked was silently replaced by the defaults at the exact moment you clicked Start again. Reported in those words. | Whatever is selected on the builder — the modes *and* the whole WHO section — survives a reload. **Built:** the builder's state is written to a `kith_builder` cookie on **every** POST, refusals included (a redirect carries `Set-Cookie` as happily as a session does), and read back defensively — the cookie is user input, so unknown mode keys, out-of-range budgets and non-dict junk fall back to the recommended default rather than a 500. Expiry 180 days, `httponly`, `samesite=lax`, `secure` following the session cookie's setting (off on loopback, on behind Caddy). **Evidence:** on the demo cabinet in a browser — arrival reads `modes=mc4,recall_self min=10 new=3`, one run started with only multiple choice ticked at 20 minutes and 5 new faces, then back to the builder: `modes=mc4 min=20 new=5`. **104 tests** (3 new, one of which posts the refused "nothing is due" form and asserts the tick survives it). Shipped as `localhost/kith:0.5.2`. |
| **M4a** ✅ **built 2026-10-02** | First half of the mode set: `reverse` (name → face), `listen` (spoken, the name never printed anywhere), `confusion` (the options are the names you actually swap), and the adaptive ladder rebuilt on **evidence** — two ladders, one per card direction. No migration. | Ticking a mode outside what you ticked is impossible; a settled face climbs one rung at a time and a miss drops it one; reverse and listening are asked on the `name_to_face` card, which is created lazily and only for faces you have already met. **Built:** `app/modes/{reverse,listen,confusion}.py` + `app/ladder.py`; `choose_mode` now takes the direction and the previous answer, `confusion_map()` reads the worst pairs straight from the review log, and `build_session` schedules two directions — the second card per person that `card.direction` always allowed but M2 never created. **Evidence:** in a browser on the demo cabinet — reverse showed *"Clara"* with four face tiles and **zero captions**, a tap graded into the person's `name_to_face` card and the verdict revealed the name only *after* the answer; listening spoke the name with *"Say it again / Show the name"* controls and the name appeared nowhere in the page; the drill asked a planted pair (Rafa / Yana) and offered **only the 5 faces with logged mix-ups**, not the cabinet's 26. Two bugs found only by that browser run: a card created inside the session was due microseconds *after* the session's own `now`, so the first reverse run found nothing to ask; and face→name cards were queued for a run that allowed none. **112 tests** (8 new; the old ladder test rewritten, because its premise — that a counter can tell you what you know — was the thing that was wrong). Shipped as `localhost/kith:0.6.0`. |
| **M4b** ✅ **built 2026-10-02** | The rest of the mode set: `match3` / `match6` — one item, three or six people, every face carrying a name you place on it. Needed **schema 5**: a board stores its own roster. The `context` mode listed here until 2026-10-02 stays **dropped** — it appears nowhere else in either document, so building it would have meant inventing a mode and calling it specified. | Drag-matching works with mouse and finger, and tap-tap always works without either. **Built:** `app/modes/match.py`, migration `0005_match_board.sql` (a `people` roster column on `session_item`), `static/js/match.js`, `static/css/match.css`. **The board is the roster, at both ends:** the item stores which people were on it, because a roster re-picked at answer time would follow whatever had been asked in between — you would answer a board you were never shown — and the companions come from mc4's own distractor rule (same group first, then lookalikes), since a board of strangers tests nothing. **Graded per member, not per item:** one queue item, one ladder schedule, but a review per person on the board, so a misplaced name is written down as *both* halves of the confusion (each lands in the other's `chosen_person_id`) — the very signal the drill is built from, and invisible if only the target were scheduled. A companion with no card is shown and graded but never scheduled, and no card is created for it: a face is introduced by a study card, never through the back door of someone else's board. A partial board is refused rather than guessed at (409, like an empty multiple-choice tap), and `tiers_for` sorts by `demand`, so the match modes are ordinary rungs — a settled face climbs mc4 → match3 one step at a time. One gesture code path (pointer events cover mouse, finger and pen; `touch-action: none`); tap-tap is always available, and the server never learns which was used, because the answer is a mapping. **Evidence:** in a browser on the demo cabinet — the builder offered 8 modes, the run screen read `data-mode: match3`, and the board rendered 3 faces, 3 name chips, **0 captions**; one name was **dragged** (synthetic pointer events, landed on the intended face) and the other two tap-tapped; a deliberately rotated board graded as three wrong rows naming both people each ("Sofia Almeida — you put Freya here"), and the database recorded **3 reviews from that single item** — `chosen_person_id` 11→21, 25→11, 21→25, each on its own card, item `requeued`. **119 tests** (6 new in `tests/test_match.py`, plus a schema assertion for 0005). Shipped as `localhost/kith:0.7.0`. |
| **M5** | Stats, calibration, printable cheat sheet, backup/restore, PWA install, i18n, polish. | Install on the phone; print a sheet for a group; download and re-import the DB. |
| **Stretch** | Leech-rescue flow, feature markers, photo-variety mode, multi-photo scheduling, SQLCipher. | — |

---

## 18. Risks and mitigations

| Risk | Mitigation |
|---|---|
| **Scheduler over-fits its own beliefs** (never asks what it thinks you know → silent rot) | The 10 % maintenance floor (§9.1) + the calibration view that surfaces "confident but wrong". |
| **A confusing app UI for the one screen that matters** (the trainer) | The trainer has no chrome, no navigation, and an escape hatch (`End session`) always visible; keyboard-complete; E2E-tested. |
| **DB grows with photos** | Import-time downscale + thumbnails + a storage line on the stats page; ~300 MB at 1500 photos. |
| **SQLite write contention** | Non-issue for one user; WAL + `busy_timeout=5000` anyway. |
| **SELinux relabel / bind-mount ownership** (`:Z` EPERM, or SQLite `readonly database`) | Data dir created with `sudo install -d -o barn -g barn` (never plain `sudo mkdir`), kept outside `/home`, rootless `UserNS=keep-id:uid=10001,gid=10001`, and an M0 check that `podman exec kith test -w /data` passes. App log names §16 when SQLite cannot write. |
| **A silently ignored HEALTHCHECK** (Podman 5 builds OCI format by default and drops it) | Build with `--format docker`; the Quadlet `HealthCmd=` is the belt to that braces, and `podman inspect kith --format {{.State.Health.Status}}` must say `healthy`. |
| **Image built as the wrong user** (rootful build → invisible to the tenant's unit) | Always build as the tenant: `sudo -u barn XDG_RUNTIME_DIR=/run/user/1001 podman build …`, since rootless image storage is per-user. |
| **FSRS API drift on upgrade** | Version pinned + hash-locked; the §15 round-trip test fails loudly on a breaking change, and the scheduler wrapper is the only file that imports `fsrs`. |
| **Timezone / DST confusion** | Store UTC only; convert for display; never store local times. |
| **Losing years of review history to a botched migration** | Every migration runs in a transaction; `PRAGMA user_version` versioning; backup endpoint; the DB is one copyable file. |
| **Session interrupted mid-run** | Server-side queue → resume works from any device. |
| **Print/PDF fragile across browsers** | The cheat sheet is print-CSS HTML (no headless-Chrome dependency); verified in the M5 acceptance test. |

---

## 19. Decisions (settled 2026-10-01)

Every question was decided on 2026-10-01: all defaults accepted, with two changes from Kevin
— **UI language English only** (Q15) and **Podman + Quadlet** instead of Docker Compose (Q9).
Recorded here so the reasoning stays visible; each is a small, contained edit to change.

| # | Question | Decision |
|---|---|---|
| Q9 | Container runtime | **Podman + Quadlet** — rootless, `systemctl --user`, `loginctl enable-linger` |
| Q10 | Keep full-resolution originals alongside the downscaled version? | **No** — downscale on import only |
| Q11 | Bind mount or named volume? | **Bind mount** `/srv/kith/data` (keeps the single file visible) |
| Q12 | Auth needed even when loopback-only? | **Off locally, on behind Caddy** |
| Q13 | One card per person, or per (person, direction)? | **Per (person, direction)** |
| Q14 | Cheat sheet in M5 or earlier? | **M5** — it is ~30 lines of HTML |
| Q15 | Localization | **English only**, but from a single string table from day one |

Nothing here blocks M0. Whatever remains is implementation detail we can settle while
building (e.g. whether the cheat sheet also gets a PDF endpoint).

---

## 20. Sources

* FSRS / py-fsrs — reference implementation and API: <https://github.com/open-spaced-repetition/py-fsrs>
  (behaviour in §2 verified locally against `fsrs` 6.3.2).
* Latimier, Peyre & Ramus (2021), *A Meta-Analytic Review of the Benefit of Spacing out
  Retrieval Practice Episodes on Retention* — spaced vs massed *g* = 0.74; expanding vs
  uniform *g* = 0.034. <https://eric.ed.gov?id=EJ1310148>
* Rawson & Dunlosky, successive relearning (retrieval + spacing, recall to a criterion):
  <https://www.retrievalpractice.org/strategies/2018/successive-relearning>
* Cepeda et al., optimal spacing gap ≈ 10–20 % of the retention interval:
  <https://pdf.retrievalpractice.org/spacing/Carpenter_etal_2012_EDPR.pdf>
* Landauer & Bjork (1978), *Optimum rehearsal patterns and name learning* — the original
  name-learning spacing study that the in-session loop follows.
* Sana & Yan (2022), interleaved retrieval practice beats blocked:
  <https://pdf.retrievalpractice.org/spacing/InterleavedRetrievalPracticePromotesScienceLearning_SanaYan_2022.pdf>
* Sondergaard et al. (2022), *Multiple producible cues do not aid face–name memory* —
  justification for point-and-click and optional writing:
  <https://www.sciencedirect.com/science/article/pii/S0001691822000464>
* Spaced retrieval protocol (original face–name application; expanding intervals, correct
  on error, back off after failure): <https://tactustherapy.com/spaced-retrieval-training-memory>
* Cueing hierarchy: <https://tactustherapy.com/cueing-hierarchy-word-finding-aphasia/>
* Recognition vs recall (cues; why multiple choice is the easier rung):
  <https://www.nngroup.com/articles/recognition-and-recall>
* SQLite BLOB performance (blobs faster than the filesystem for small objects, ~20 % less
  space at 10 KB): <https://www.sqlite.org/fasterthanfs.html> and practitioner experience:
  <https://sqlite.org/forum/info/a02a8bc478cee744d8269c7a56c867bdc3706f5b5c099aa1c09f6bf65a9774a3>
* Host deployment pattern (Caddy, own login, firewall, hairpin pitfall): internal skill
  `barn-host-webapp-deploy`.
