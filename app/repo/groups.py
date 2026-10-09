"""Groups: arbitrarily overlapping sets of people you want to train on together."""

from __future__ import annotations

import sqlite3

from app import i18n
from app.db import utcnow
from app.repo import people as people


class InvalidGroup(ValueError):
    """The message is shown to the user as-is."""


def listing(conn: sqlite3.Connection) -> list[dict]:
    """All groups with their member counts, biggest first then alphabetical."""
    rows = conn.execute(
        """
        SELECT g.id, g.name, g.note, g.created_at,
               (SELECT count(*) FROM grp_member m WHERE m.grp_id = g.id) AS members
        FROM grp g
        ORDER BY members DESC, g.name COLLATE NOCASE
        """
    )
    return [dict(row) for row in rows]


def get(conn: sqlite3.Connection, group_id: int) -> dict | None:
    row = conn.execute(
        """
        SELECT g.id, g.name, g.note, g.created_at,
               (SELECT count(*) FROM grp_member m WHERE m.grp_id = g.id) AS members
        FROM grp g WHERE g.id = ?
        """,
        (group_id,),
    ).fetchone()
    return dict(row) if row else None


def members(conn: sqlite3.Connection, group_id: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT p.id, p.nickname, p.status, m.added_at, p.family_name,
               {name} AS display_name,
               EXISTS (SELECT 1 FROM photo f WHERE f.person_id = p.id) AS has_photo,
               {photo} 
        FROM grp_member m JOIN person p ON p.id = m.person_id
        WHERE m.grp_id = ?
        ORDER BY {name} COLLATE NOCASE
        """.format(name=people.RECALL_NAME, photo=people.PHOTO_ID_SQL),
        (group_id,),
    )
    return [dict(row) for row in rows]


def member_ids(conn: sqlite3.Connection, group_id: int) -> list[int]:
    rows = conn.execute("SELECT person_id FROM grp_member WHERE grp_id = ?", (group_id,))
    return [int(row["person_id"]) for row in rows]


def groups_for_person(conn: sqlite3.Connection, person_id: int) -> list[dict]:
    """Groups this person belongs to — used to tick the right checkboxes."""
    rows = conn.execute(
        """
        SELECT g.id, g.name
        FROM grp_member m JOIN grp g ON g.id = m.grp_id
        WHERE m.person_id = ?
        ORDER BY g.name COLLATE NOCASE
        """,
        (person_id,),
    )
    return [dict(row) for row in rows]


def create(conn: sqlite3.Connection, name: str, note: str | None = None) -> int:
    clean = _clean_name(name)
    try:
        cursor = conn.execute(
            "INSERT INTO grp (name, note, created_at) VALUES (?, ?, ?)",
            (clean, (note or "").strip() or None, utcnow()),
        )
    except sqlite3.IntegrityError as exc:
        raise InvalidGroup(i18n.t("groups.error.duplicate", name=clean)) from exc
    return int(cursor.lastrowid or 0)


def update(conn: sqlite3.Connection, group_id: int, *, name: str, note: str | None = None) -> bool:
    clean = _clean_name(name)
    try:
        changed = conn.execute(
            "UPDATE grp SET name = ?, note = ? WHERE id = ?",
            (clean, (note or "").strip() or None, group_id),
        ).rowcount
    except sqlite3.IntegrityError as exc:
        raise InvalidGroup(i18n.t("groups.error.duplicate", name=clean)) from exc
    return changed > 0


def delete(conn: sqlite3.Connection, group_id: int) -> bool:
    return conn.execute("DELETE FROM grp WHERE id = ?", (group_id,)).rowcount > 0


def set_members(conn: sqlite3.Connection, group_id: int, person_ids: list[int]) -> None:
    """Replace the membership wholesale — the form posts the complete ticked set.

    Only the difference is written, so saving a large group does not rewrite it all.
    """
    current = set(member_ids(conn, group_id))
    wanted = {int(person_id) for person_id in person_ids}
    now = utcnow()

    conn.execute("SAVEPOINT set_members")
    try:
        for person_id in sorted(wanted - current):
            conn.execute(
                "INSERT OR IGNORE INTO grp_member (grp_id, person_id, added_at) VALUES (?, ?, ?)",
                (group_id, person_id, now),
            )
        for person_id in sorted(current - wanted):
            conn.execute(
                "DELETE FROM grp_member WHERE grp_id = ? AND person_id = ?",
                (group_id, person_id),
            )
        conn.execute("RELEASE set_members")
    except sqlite3.Error:
        conn.execute("ROLLBACK TO set_members")
        conn.execute("RELEASE set_members")
        raise


def set_groups_for_person(conn: sqlite3.Connection, person_id: int, group_ids: list[int]) -> None:
    """Replace one person's group memberships — the person form posts the ticked set."""
    current = {group["id"] for group in groups_for_person(conn, person_id)}
    wanted = {int(group_id) for group_id in group_ids}
    now = utcnow()

    conn.execute("SAVEPOINT set_groups")
    try:
        for group_id in sorted(wanted - current):
            conn.execute(
                "INSERT OR IGNORE INTO grp_member (grp_id, person_id, added_at) VALUES (?, ?, ?)",
                (group_id, person_id, now),
            )
        for group_id in sorted(current - wanted):
            conn.execute(
                "DELETE FROM grp_member WHERE grp_id = ? AND person_id = ?",
                (group_id, person_id),
            )
        conn.execute("RELEASE set_groups")
    except sqlite3.Error:
        conn.execute("ROLLBACK TO set_groups")
        conn.execute("RELEASE set_groups")
        raise


def _clean_name(name: str) -> str:
    clean = (name or "").strip()
    if not clean:
        raise InvalidGroup(i18n.t("groups.error.name_required"))
    if len(clean) > 80:
        raise InvalidGroup(i18n.t("groups.error.name_too_long"))
    return clean
