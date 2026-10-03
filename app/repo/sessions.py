"""Sessions and their server-side queue.

The queue lives here rather than in the browser, and that is what makes a training run
survive a closed tab, a locked phone or a container restart: `GET /api/sessions/{id}`
rebuilds the whole screen from this state.

Ordering is `position`, a plain integer with gaps allowed on purpose — a requeued item
slots in at the midpoint between two neighbours, so no other row has to move. When two
neighbours are adjacent, the session's positions are respaced; that is safe precisely
because `position` is not unique (which is why it is a separate column from `seq`).
"""

from __future__ import annotations

import json
import sqlite3

STEP = 10  # spacing between neighbours in a freshly built queue

OPEN_STATES = ("pending", "requeued")


def create(
    conn: sqlite3.Connection,
    *,
    modes: list[str],
    target_kind: str = "everything",
    grp_ids: list[int] | None = None,
    person_ids: list[int] | None = None,
    new_budget: int = 0,
    retire_count: int = 1,
    planned_items: int = 0,
    now: str,
) -> int:
    cursor = conn.execute(
        "INSERT INTO session (started_at, grp_ids, person_ids, modes, target_kind,"
        " new_budget, retire_count, planned_items) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            now,
            json.dumps(grp_ids) if grp_ids else None,
            json.dumps(person_ids) if person_ids else None,
            json.dumps(modes),
            target_kind,
            int(new_budget),
            int(retire_count),
            int(planned_items),
        ),
    )
    return int(cursor.lastrowid or 0)


def get(conn: sqlite3.Connection, session_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM session WHERE id = ?", (session_id,)).fetchone()
    if row is None:
        return None
    session = dict(row)
    session["modes"] = json.loads(str(session["modes"] or "[]"))
    session["grp_ids"] = json.loads(str(session["grp_ids"])) if session["grp_ids"] else []
    session["person_ids"] = (
        json.loads(str(session["person_ids"])) if session["person_ids"] else []
    )
    return session


def open_session(conn: sqlite3.Connection) -> dict | None:
    """The most recent unfinished session — what "resume where I left off" means."""
    row = conn.execute(
        "SELECT id FROM session WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return get(conn, int(row["id"])) if row else None


def add_items(conn: sqlite3.Connection, session_id: int, specs: list[dict]) -> list[int]:
    """Append queue entries in order. `specs` are dicts of card_id / new_person_id / mode.

    Numbering continues from what the session already holds — items are also added
    mid-session (a study card after "no idea"), not only when the queue is first built.
    """
    start_seq = int(
        conn.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM session_item WHERE session_id = ?", (session_id,)
        ).fetchone()[0]
    )
    start_position = int(
        conn.execute(
            "SELECT COALESCE(MAX(position), 0) FROM session_item WHERE session_id = ?",
            (session_id,),
        ).fetchone()[0]
    )
    ids: list[int] = []
    for index, spec in enumerate(specs, start=1):
        board = spec.get("people")
        cursor = conn.execute(
            "INSERT INTO session_item (session_id, seq, position, card_id, new_person_id, mode,"
            " people) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                start_seq + index,
                start_position + index * STEP,
                spec.get("card_id"),
                spec.get("new_person_id"),
                str(spec["mode"]),
                json.dumps(board) if board else None,
            ),
        )
        ids.append(int(cursor.lastrowid or 0))
    return ids


def _row(row: sqlite3.Row) -> dict:
    """One queue row, with a board's roster decoded back out of its JSON column.

    Tolerant on purpose: a roster that will not parse means a board cannot be shown, and the
    caller skips the item rather than taking the whole session down with it.
    """
    item = dict(row)
    try:
        item["people"] = json.loads(str(item["people"])) if item.get("people") else []
    except (TypeError, ValueError):
        item["people"] = []
    return item


def items(conn: sqlite3.Connection, session_id: int, *, states: tuple[str, ...] | None = None) -> list[dict]:
    sql = "SELECT * FROM session_item WHERE session_id = ?"
    params: list[object] = [session_id]
    if states:
        sql += f" AND state IN ({','.join('?' for _ in states)})"
        params.extend(states)
    sql += " ORDER BY position, seq"
    return [_row(row) for row in conn.execute(sql, params)]


def open_items(conn: sqlite3.Connection, session_id: int) -> list[dict]:
    return items(conn, session_id, states=OPEN_STATES)


def item(conn: sqlite3.Connection, item_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM session_item WHERE id = ?", (item_id,)).fetchone()
    return _row(row) if row else None


def next_item(conn: sqlite3.Connection, session_id: int) -> dict | None:
    """The item to ask next: lowest position among the still-open ones."""
    row = conn.execute(
        "SELECT * FROM session_item WHERE session_id = ? AND state IN ('pending','requeued')"
        " ORDER BY position, seq LIMIT 1",
        (session_id,),
    ).fetchone()
    return _row(row) if row else None


def mark_answered(conn: sqlite3.Connection, item_id: int, *, correct: bool) -> None:
    conn.execute(
        "UPDATE session_item SET state = 'answered', correct_count = correct_count + ?"
        " WHERE id = ?",
        (1 if correct else 0, item_id),
    )


def _respace(conn: sqlite3.Connection, session_id: int) -> None:
    """Re-lay the session's positions with even gaps (safe: `position` is not unique)."""
    for index, row in enumerate(items(conn, session_id)):
        conn.execute(
            "UPDATE session_item SET position = ? WHERE id = ?", ((index + 1) * STEP, int(row["id"]))
        )


def requeue(conn: sqlite3.Connection, item_id: int, *, after_n: int = 5) -> None:
    """Put a missed item back in the queue, `after_n` items from where it is now.

    This is the successive-relearning part (§8.2): correct immediately, then ask again a
    little later, rather than repeating it back-to-back.
    """
    row = item(conn, item_id)
    if row is None:
        return
    session_id = int(row["session_id"])

    def slot() -> int:
        others = [r for r in open_items(conn, session_id) if int(r["id"]) != item_id]
        if not others:
            return int(row["position"]) + STEP
        index = min(max(after_n, 0), len(others) - 1)
        anchor = int(others[index]["position"])
        following = others[index + 1] if index + 1 < len(others) else None
        if following is None:
            return anchor + STEP
        return (anchor + int(following["position"])) // 2

    position = slot()
    neighbours = sorted(int(r["position"]) for r in open_items(conn, session_id))
    if position in neighbours:  # no room between them: respace and try once more
        _respace(conn, session_id)
        row = item(conn, item_id) or row
        position = slot()

    conn.execute(
        "UPDATE session_item SET state = 'requeued', requeues = requeues + 1, position = ?"
        " WHERE id = ?",
        (position, item_id),
    )


def counts(conn: sqlite3.Connection, session_id: int) -> dict[str, int]:
    """Progress as **items retired**, not answers given.

    A missed item is asked again later in the same session, so counting submissions would
    let "12 of 12" appear after twenty taps. `answers` is kept separately for the summary,
    where per-answer accuracy is the meaningful number.
    """
    session = get(conn, session_id)
    if session is None:
        return {"planned": 0, "retired": 0, "answers": 0, "correct": 0, "remaining": 0}
    rows = conn.execute(
        "SELECT mode, state FROM session_item WHERE session_id = ?", (session_id,)
    ).fetchall()
    graded = [row for row in rows if str(row["mode"]) != "study"]
    remaining = sum(1 for row in graded if str(row["state"]) in OPEN_STATES)
    planned = int(session["planned_items"]) or len(graded)
    return {
        "planned": planned,
        "retired": max(0, planned - remaining),
        "answers": int(session["answered"]),
        "correct": int(session["correct"]),
        "remaining": remaining,
    }


def record_answer(conn: sqlite3.Connection, session_id: int, *, correct: bool) -> None:
    conn.execute(
        "UPDATE session SET answered = answered + 1, correct = correct + ? WHERE id = ?",
        (1 if correct else 0, session_id),
    )


def end(conn: sqlite3.Connection, session_id: int, *, now: str) -> None:
    conn.execute(
        "UPDATE session SET ended_at = COALESCE(ended_at, ?) WHERE id = ?", (now, session_id)
    )


def recent(conn: sqlite3.Connection, limit: int = 5) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM session ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(row) for row in rows]
