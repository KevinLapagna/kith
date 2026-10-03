"""Backup, restore, and the sheet you print before a party.

The two directions of the single-file promise: what comes out has to be a database another
Kith could open, and what comes back in must not be able to wreck a cabinet by being the
wrong file.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app import config, db
from app.repo import groups as groups_repo


def _seed(conn, people: int = 3) -> list[int]:
    """A cabinet with photos, so a sheet has faces to print. Returns the new person ids."""
    now = db.utcnow()
    ids: list[int] = []
    for index in range(people):
        cursor = conn.execute(
            "INSERT INTO person (display_name, given_name, family_name, status, created_at,"
            " updated_at) VALUES (?, ?, ?, 'active', ?, ?)",
            (f"Person {index}", f"Person{index}", f"Family{index}", now, now),
        )
        person_id = int(cursor.lastrowid or 0)
        conn.execute(
            "INSERT INTO photo (person_id, is_primary, mime, width, height, bytes, sha256,"
            " data, thumb, created_at) VALUES (?, 1, 'image/webp', 8, 8, 64, ?, ?, ?, ?)",
            (person_id, f"sha-{index}-{now}", b"", b"", now),
        )
        ids.append(person_id)
    return ids


def _download(client) -> bytes:
    response = client.get("/api/backup")
    assert response.status_code == 200, response.text
    return response.content


def test_the_backup_is_a_database_another_kith_could_open(settings, client, tmp_path):
    with db.connect(settings.db_path) as conn:
        db.migrate(conn)
        _seed(conn)

    copy = tmp_path / "from-the-app.sqlite"
    copy.write_bytes(_download(client))

    probe = sqlite3.connect(copy)
    try:
        assert probe.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert int(probe.execute("PRAGMA user_version").fetchone()[0]) == config.SCHEMA_VERSION
        assert probe.execute("SELECT count(*) FROM person").fetchone()[0] == 3
    finally:
        probe.close()


def test_the_sheet_shows_one_group_and_its_names(settings, client):
    with db.connect(settings.db_path) as conn:
        db.migrate(conn)
        ids = _seed(conn)
        group_id = groups_repo.create(conn, "Guests")
        groups_repo.set_members(conn, group_id, ids[:2])

    everyone = client.get("/sheet")
    assert everyone.status_code == 200, everyone.text
    assert "Person 0" in everyone.text and "Person 2" in everyone.text
    assert "Family1" in everyone.text, "the family name prints small, but it prints"

    guests = client.get(f"/sheet?group={group_id}")
    assert guests.status_code == 200, guests.text
    assert "Guests" in guests.text
    assert "Person 2" not in guests.text, "a group sheet is exactly the people who will be there"


def _post_import(client, name: str, body: bytes, confirm: str):
    return client.post(
        "/api/import",
        files={"database": (name, body, "application/vnd.sqlite3")},
        data={"confirm": confirm},
        follow_redirects=False,
    )


def test_import_without_the_typed_word_changes_nothing(settings, client, tmp_path):
    """The destructive path is guarded by an act of attention, not by a second click."""
    with db.connect(settings.db_path) as conn:
        db.migrate(conn)
        _seed(conn, 2)
    copy = tmp_path / "copy.sqlite"
    copy.write_bytes(_download(client))

    response = _post_import(client, "copy.sqlite", copy.read_bytes(), "yes please")

    assert response.status_code == 303
    assert response.headers["location"] == "/stats?data=confirm"
    with db.connect(settings.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM person").fetchone()[0] == 2


def test_a_file_that_is_not_a_database_is_refused(settings, client):
    with db.connect(settings.db_path) as conn:
        db.migrate(conn)

    response = _post_import(client, "holiday.jpg", b"\xff\xd8\xff\xe0 not a database", "REPLACE")

    assert response.headers["location"] == "/stats?data=notsqlite"


def test_a_good_copy_replaces_the_cabinet(settings, client, tmp_path):
    """The whole promise: take a copy, lose everything, put the copy back."""
    with db.connect(settings.db_path) as conn:
        db.migrate(conn)
        _seed(conn, 3)
    copy = tmp_path / "keep-me.sqlite"
    copy.write_bytes(_download(client))

    with db.connect(settings.db_path) as conn:
        conn.execute("DELETE FROM photo")
        conn.execute("DELETE FROM person")
        assert conn.execute("SELECT count(*) FROM person").fetchone()[0] == 0

    response = _post_import(client, "keep-me.sqlite", copy.read_bytes(), "replace")

    assert response.status_code == 303, response.text
    assert response.headers["location"] == "/stats?data=imported"
    with db.connect(settings.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM person").fetchone()[0] == 3
        assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == config.SCHEMA_VERSION
    assert list(Path(settings.data_dir).glob("replaced-*.sqlite")), (
        "the database being replaced is snapshotted before the swap"
    )

