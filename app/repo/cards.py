"""Cards: one per (person, direction), with the FSRS state kept as JSON.

Two rules keep this module honest:

  * **The FSRS blob is the authority.** `stability`, `difficulty`, `state` and `due` are
    projections of it, written only by `save_fsrs`, so a query can index `due` without
    anyone having to remember to update two places. A test asserts they never diverge.
  * **`Card.from_dict()` wants strings, not datetimes** (verified against fsrs 6.3.2):
    it calls `datetime.fromisoformat(...)` on the value, so the round-trip is
    `json.dumps(card.to_dict(), default=str)` → store → `Card.from_dict(json.loads(...))`.
    Handing it real datetimes raises TypeError — a scheduler-corrupting bug that would
    only show up later, hence the dedicated regression test.
"""

from __future__ import annotations

import json
import sqlite3

from fsrs import Card, Scheduler

from app.db import utcnow

FACE_TO_NAME = "face_to_name"
NAME_TO_FACE = "name_to_face"
DIRECTIONS = (FACE_TO_NAME, NAME_TO_FACE)


def make_scheduler(desired_retention: float = 0.90) -> Scheduler:
    """A scheduler for a single user needs no tuned parameters (§8.3)."""
    return Scheduler(desired_retention=desired_retention)


def load(row: sqlite3.Row | dict) -> Card:
    """Rebuild the FSRS card from its stored JSON."""
    return Card.from_dict(json.loads(str(row["fsrs"])))


def dump(card: Card) -> str:
    """Serialise for storage — `default=str` turns the datetimes into ISO strings."""
    return json.dumps(card.to_dict(), default=str)


def get(conn: sqlite3.Connection, card_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM card WHERE id = ?", (card_id,)).fetchone()
    return dict(row) if row else None


def for_person(
    conn: sqlite3.Connection, person_id: int, direction: str = FACE_TO_NAME
) -> dict | None:
    row = conn.execute(
        "SELECT * FROM card WHERE person_id = ? AND direction = ?", (person_id, direction)
    ).fetchone()
    return dict(row) if row else None


def ensure(
    conn: sqlite3.Connection, person_id: int, direction: str = FACE_TO_NAME
) -> dict:
    """The card for this person and direction, created on first use.

    Cards are made lazily rather than by a trigger on `person`: a person you never train
    on costs nothing, and adding a person can never fail because of the scheduler.
    """
    existing = for_person(conn, person_id, direction)
    if existing is not None:
        return existing

    card = Card()
    now = utcnow()
    data = card.to_dict()
    cursor = conn.execute(
        "INSERT INTO card (person_id, direction, fsrs, due, stability, difficulty, state,"
        " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            person_id,
            direction,
            dump(card),
            str(data.get("due")),
            data.get("stability"),
            data.get("difficulty"),
            int(data.get("state") or 0),
            now,
            now,
        ),
    )
    created = get(conn, int(cursor.lastrowid or 0))
    assert created is not None  # just inserted
    return created


def save_fsrs(conn: sqlite3.Connection, card_id: int, card: Card, *, now: str | None = None) -> None:
    """Write the reviewed card back, keeping the indexed projections in step."""
    data = card.to_dict()
    conn.execute(
        "UPDATE card SET fsrs = ?, due = ?, stability = ?, difficulty = ?, state = ?,"
        " updated_at = ? WHERE id = ?",
        (
            dump(card),
            str(data.get("due")),
            data.get("stability"),
            data.get("difficulty"),
            int(data.get("state") or 0),
            now or utcnow(),
            card_id,
        ),
    )


def bump_reps_lapses(
    conn: sqlite3.Connection, card_id: int, *, lapsed: bool, now: str | None = None
) -> None:
    conn.execute(
        "UPDATE card SET reps = reps + 1,"
        " lapses = lapses + (CASE WHEN ? THEN 1 ELSE 0 END), updated_at = ?"
        " WHERE id = ?",
        (1 if lapsed else 0, now or utcnow(), card_id),
    )


def due(
    conn: sqlite3.Connection,
    *,
    now: str,
    limit: int = 200,
    group_id: int | None = None,
) -> list[dict]:
    """Cards whose due time has passed, for active people, oldest first.

    Ordering here is deliberately plain (most overdue first); the *interesting* ordering
    is the selection engine's priority score in M3. This is the pool it will score.
    """
    sql = [
        "SELECT c.*, p.status, p.family_name, " + people.RECALL_NAME + " AS display_name FROM card c",
        "JOIN person p ON p.id = c.person_id",
        "WHERE c.suspended = 0 AND p.status = 'active' AND c.due <= ?",
    ]
    params: list[object] = [now]
    if group_id is not None:
        sql.append(
            "AND EXISTS (SELECT 1 FROM grp_member m WHERE m.person_id = c.person_id"
            " AND m.grp_id = ?)"
        )
        params.append(group_id)
    sql.append("ORDER BY c.due LIMIT ?")
    params.append(limit)
    return [dict(row) for row in conn.execute(" ".join(sql), params)]


def suspended(conn: sqlite3.Connection, card_id: int, value: bool, *, now: str | None = None) -> None:
    conn.execute(
        "UPDATE card SET suspended = ?, updated_at = ? WHERE id = ?",
        (1 if value else 0, now or utcnow(), card_id),
    )


def counts(conn: sqlite3.Connection, *, now: str) -> dict[str, int]:
    row = conn.execute(
        """
        SELECT
          count(*)                                                   AS total,
          sum(CASE WHEN due <= ? AND suspended = 0 THEN 1 ELSE 0 END) AS due,
          sum(CASE WHEN reps = 0 THEN 1 ELSE 0 END)                   AS fresh,
          sum(CASE WHEN lapses > 0 THEN 1 ELSE 0 END)                 AS shaky
        FROM card
        """,
        (now,),
    ).fetchone()
    return {key: int(row[key] or 0) for key in ("total", "due", "fresh", "shaky")}
