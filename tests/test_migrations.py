"""Migrations and schema — the contract that makes later milestones safe."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app import config, db


def test_every_migration_is_numbered_and_unique() -> None:
    migrations = db.discover_migrations()
    assert migrations, "no migrations found"
    assert [v for v, _ in migrations] == sorted(v for v, _ in migrations)


def test_schema_version_constant_matches_highest_migration() -> None:
    """Guards the classic mistake: adding a migration but forgetting SCHEMA_VERSION,
    which would make the health check report a healthy database at the wrong version."""
    highest = max(v for v, _ in db.discover_migrations())
    assert config.SCHEMA_VERSION == highest


def test_malformed_migration_name_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "init.sql").write_text("SELECT 1;")
    with pytest.raises(RuntimeError, match="must start with a number"):
        db.discover_migrations(tmp_path)


def test_migrate_applies_and_bumps_version(settings: config.Settings) -> None:
    expected = [path.name for _, path in db.discover_migrations()]
    with db.connect(settings.db_path) as conn:
        assert db.current_version(conn) == 0
        applied = db.migrate(conn)
        assert applied == expected
        assert db.current_version(conn) == config.SCHEMA_VERSION

        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {"app_meta", "setting", "person"} <= tables
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_m1_schema_objects_exist(settings: config.Settings) -> None:
    """0002 is the first migration containing `CREATE TRIGGER ... BEGIN ... END;` bodies.

    If the migration runner ever went back to naively splitting on semicolons, those
    triggers would arrive at SQLite as fragments and this test would fail — which is the
    point of asserting on them explicitly rather than only on the tables.
    """
    with db.connect(settings.db_path) as conn:
        db.migrate(conn)
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {"photo", "grp", "grp_member", "tag", "person_tag", "person_fts"} <= tables

        triggers = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")
        }
        assert {"person_fts_insert", "person_fts_update", "person_fts_delete"} <= triggers

        indexes = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        }
        assert {"photo_sha256", "photo_person", "grp_member_person"} <= indexes


def test_fts_triggers_keep_the_index_in_sync(settings: config.Settings) -> None:
    """Insert, rename, delete — the documented external-content FTS5 dance, verified."""
    with db.connect(settings.db_path) as conn:
        db.migrate(conn)
        now = db.utcnow()
        conn.execute(
            "INSERT INTO person (display_name, created_at, updated_at) VALUES (?, ?, ?)",
            ("Magdalena Vogt", now, now),
        )
        assert conn.execute(
            "SELECT count(*) FROM person_fts WHERE person_fts MATCH ?", ('"vogt"*',)
        ).fetchone()[0] == 1

        conn.execute("UPDATE person SET display_name = ? WHERE display_name = ?",
                     ("Magdalena Frei", "Magdalena Vogt"))
        assert conn.execute(
            "SELECT count(*) FROM person_fts WHERE person_fts MATCH ?", ('"vogt"*',)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT count(*) FROM person_fts WHERE person_fts MATCH ?", ('"frei"*',)
        ).fetchone()[0] == 1

        conn.execute("DELETE FROM person WHERE display_name = ?", ("Magdalena Frei",))
        assert conn.execute(
            "SELECT count(*) FROM person_fts WHERE person_fts MATCH ?", ('"frei"*',)
        ).fetchone()[0] == 0


def test_0004_backfills_the_first_name_and_reindexes_the_name_you_type(
    settings: config.Settings, tmp_path: Path
) -> None:
    """The shape Kevin's live cabinet is in: one word in display_name, nothing in given_name.

    After the migration the first name is filled in (so the form has something to show) and the
    search index holds the name you would actually type — the resolved one, plus the family name.
    The index check is the interesting half: an external-content FTS5 table rebuilds from the
    *raw* columns, so 'rebuild' would have indexed the empty override and found nothing.
    """
    legacy = tmp_path / "migrations"
    legacy.mkdir()
    for version, path in db.discover_migrations():
        if version >= 4:
            break
        (legacy / path.name).write_text(path.read_text())

    with db.connect(settings.db_path) as conn:
        db.migrate(conn, directory=legacy)
        now = db.utcnow()
        conn.execute(
            "INSERT INTO person (display_name, created_at, updated_at) VALUES (?, ?, ?)",
            ("Rafa", now, now),
        )
        conn.execute(
            "INSERT INTO person (display_name, given_name, family_name, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?)",
            ("", "Yana", "Weiss", now, now),
        )

        applied = db.migrate(conn)
        # 0004 is the one this test is about; later migrations ride along in the same run.
        assert applied[0].startswith("0004"), applied

        rafa = conn.execute(
            "SELECT given_name FROM person WHERE display_name = 'Rafa'"
        ).fetchone()
        assert rafa["given_name"] == "Rafa", "a one-word name becomes the first name"

        for probe in ("rafa", "yana", "weiss"):
            hits = conn.execute(
                "SELECT count(*) FROM person_fts WHERE person_fts MATCH ?", (f'"{probe}"*',)
            ).fetchone()[0]
            assert hits == 1, f"the index must answer for {probe!r}"


def test_0005_adds_the_board_roster_column(settings: config.Settings) -> None:
    """A board item stores its roster: the board on screen must be the board graded, and a
    roster recomputed at answer time would follow whatever was asked in the meantime."""
    with db.connect(settings.db_path) as conn:
        db.migrate(conn)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(session_item)")}
    assert "people" in columns


def test_migrate_is_idempotent(settings: config.Settings) -> None:
    with db.connect(settings.db_path) as conn:
        db.migrate(conn)
        assert db.migrate(conn) == []          # second run applies nothing
        assert db.current_version(conn) == config.SCHEMA_VERSION


def test_seeded_settings_are_present(settings: config.Settings) -> None:
    with db.connect(settings.db_path) as conn:
        db.migrate(conn)
        settings_rows = {
            row["key"]: row["value"]
            for row in conn.execute("SELECT key, value FROM setting")
        }
    assert settings_rows["desired_retention"] == "0.90"
    assert settings_rows["new_face_budget"] == "3"
    assert "mc4" in settings_rows["modes_enabled"]


def test_person_defaults_and_check_constraint(settings: config.Settings) -> None:
    with db.connect(settings.db_path) as conn:
        db.migrate(conn)
        conn.execute(
            "INSERT INTO person (display_name, created_at, updated_at) VALUES (?, ?, ?)",
            ("Anna Brunner", db.utcnow(), db.utcnow()),
        )
        row = conn.execute("SELECT status FROM person WHERE display_name = ?", ("Anna Brunner",)).fetchone()
        assert row["status"] == "active"

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO person (display_name, status, created_at, updated_at) VALUES (?, ?, ?, ?)",
                ("Bad Status", "bogus", db.utcnow(), db.utcnow()),
            )


def test_failed_migration_leaves_database_at_previous_version(settings: config.Settings, tmp_path: Path) -> None:
    """A broken migration must not leave a half-applied schema behind.

    Note the failure has to be a *runtime* error, not a syntax one: SQLite happily
    accepts bogus type names in non-STRICT tables, so `CREATE TABLE bad (id NOSUCHTYPE)`
    would have succeeded and quietly proven nothing. A NOT NULL violation does the job
    and also checks that the DDL in the same migration is rolled back with it.
    """
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "0001_ok.sql").write_text("CREATE TABLE good (id INTEGER PRIMARY KEY) STRICT;")
    (migrations / "0002_broken.sql").write_text(
        "CREATE TABLE bad (id INTEGER PRIMARY KEY, x TEXT NOT NULL) STRICT;\n"
        "INSERT INTO bad (id) VALUES (1);\n"  # violates NOT NULL
    )

    with db.connect(settings.db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            db.migrate(conn, directory=migrations)
        assert db.current_version(conn) == 1
        tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "good" in tables
        assert "bad" not in tables
