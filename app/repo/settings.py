"""The `setting` table: a handful of trainer knobs, one row per key.

Values are stored as text and coerced on read, so a typo in the database surfaces as a
clear ValueError at the boundary rather than as a strange number deep inside the trainer.
"""

from __future__ import annotations

import sqlite3

DEFAULTS: dict[str, str] = {
    "desired_retention": "0.90",
    "session_minutes": "10",
    "new_face_budget": "3",
    "retire_count": "1",
    "interleave_window": "4",
    "maintenance_floor": "0.10",
    "slow_factor": "1.5",
    "fast_factor": "0.6",
}


def all_values(conn: sqlite3.Connection) -> dict[str, str]:
    """Every setting, with the defaults filling in anything missing.

    Merging rather than returning the raw table means an older database (or a key added
    after it was created) still yields a complete set — the trainer never has to guard
    against a missing knob.
    """
    values = dict(DEFAULTS)
    for row in conn.execute("SELECT key, value FROM setting"):
        values[str(row["key"])] = str(row["value"])
    return values


def get(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM setting WHERE key = ?", (key,)).fetchone()
    if row is not None:
        return str(row["value"])
    return DEFAULTS.get(key, default)


def get_int(conn: sqlite3.Connection, key: str, default: int = 0) -> int:
    raw = get(conn, key)
    try:
        return int(str(raw))
    except (TypeError, ValueError):
        return default


def get_float(conn: sqlite3.Connection, key: str, default: float = 0.0) -> float:
    raw = get(conn, key)
    try:
        return float(str(raw))
    except (TypeError, ValueError):
        return default


def set_value(conn: sqlite3.Connection, key: str, value: str, *, now: str) -> None:
    conn.execute(
        "INSERT INTO setting (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, str(value), now),
    )
