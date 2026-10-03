"""The review log — append-only, and the source of every statistic in the app.

Nothing here updates: a review is written once, and everything else (the confusion
matrix, per-person accuracy, the end-of-session summary) is derived by querying it.
`chosen_person_id` is the column that makes the confusion feature possible at all: it
records which lure you actually tapped, not merely that you were wrong.
"""

from __future__ import annotations

import sqlite3

from app.repo import people as people


def insert(
    conn: sqlite3.Connection,
    *,
    card_id: int,
    mode: str,
    correct: bool,
    rating: int,
    reviewed_at: str,
    session_id: int | None = None,
    session_item_id: int | None = None,
    derived: bool = True,
    no_idea: bool = False,
    hint_level: int = 0,
    latency_ms: int | None = None,
    chosen_person_id: int | None = None,
    confidence: int | None = None,
    elapsed_days: float | None = None,
    scheduled_days: float | None = None,
    duration_ms: int | None = None,
) -> int:
    cursor = conn.execute(
        "INSERT INTO review (card_id, session_id, session_item_id, reviewed_at, rating,"
        " derived, mode, correct, no_idea, hint_level, latency_ms, chosen_person_id,"
        " confidence, elapsed_days, scheduled_days, duration_ms)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            card_id,
            session_id,
            session_item_id,
            reviewed_at,
            int(rating),
            1 if derived else 0,
            mode,
            1 if correct else 0,
            1 if no_idea else 0,
            int(hint_level),
            latency_ms,
            chosen_person_id,
            confidence,
            elapsed_days,
            scheduled_days,
            duration_ms,
        ),
    )
    return int(cursor.lastrowid or 0)


def median_latency_ms(conn: sqlite3.Connection, card_id: int, *, limit: int = 20) -> int | None:
    """This card's own typical answering time — the baseline the rating derivation needs.

    Per card, not per app: you are much slower on a face you met once than on one you
    have seen ten times, and "slow" should mean slow *for this person*.
    """
    rows = conn.execute(
        "SELECT latency_ms FROM review WHERE card_id = ? AND latency_ms IS NOT NULL"
        " ORDER BY reviewed_at DESC LIMIT ?",
        (card_id, limit),
    ).fetchall()
    values = sorted(int(row["latency_ms"]) for row in rows)
    if not values:
        return None
    middle = len(values) // 2
    if len(values) % 2:
        return values[middle]
    return (values[middle - 1] + values[middle]) // 2


def for_session(conn: sqlite3.Connection, session_id: int) -> list[dict]:
    """Every answer of a session, newest first, with the names attached."""
    rows = conn.execute(
        f"""
        SELECT r.*, p.id AS person_id, {people.RECALL_NAME} AS display_name, p.family_name,
               p.mnemonic, p.where_met, p.feature_note, c.direction
        FROM review r
        JOIN card c   ON c.id = r.card_id
        JOIN person p ON p.id = c.person_id
        WHERE r.session_id = ?
        ORDER BY r.id DESC
        """,
        (session_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def accuracy_by_person(conn: sqlite3.Connection, session_id: int) -> list[dict]:
    """Per person for one session: asked, right, and the worst first."""
    rows = conn.execute(
        f"""
        SELECT p.id AS person_id, {people.RECALL_NAME} AS display_name, p.family_name,
               count(*) AS asked,
               sum(r.correct) AS correct,
               sum(CASE WHEN r.no_idea = 1 THEN 1 ELSE 0 END) AS no_idea
        FROM review r
        JOIN card c   ON c.id = r.card_id
        JOIN person p ON p.id = c.person_id
        WHERE r.session_id = ?
        GROUP BY p.id, p.family_name, {people.RECALL_NAME}
        ORDER BY (CAST(sum(r.correct) AS REAL) / count(*)) ASC, asked DESC
        """,
        (session_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def confusion_pairs(conn: sqlite3.Connection, *, limit: int = 10) -> list[dict]:
    """The pairs you actually swap: target ↔ the lure you tapped.

    Ordered by raw count: these are the mistakes worth drilling, and they are what the
    M3 selection engine will boost.
    """
    rows = conn.execute(
        f"""
        SELECT c.person_id AS person_id, {people.RECALL_NAME} AS person_name, p.family_name,
               r.chosen_person_id AS confused_with_id,
               {people.recall_name('q')} AS confused_with_name,
               count(*) AS mixups
        FROM review r
        JOIN card c   ON c.id = r.card_id
        JOIN person p ON p.id = c.person_id
        JOIN person q ON q.id = r.chosen_person_id
        WHERE r.correct = 0 AND r.chosen_person_id IS NOT NULL
        GROUP BY c.person_id, r.chosen_person_id
        ORDER BY mixups DESC, {people.RECALL_NAME}
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]
