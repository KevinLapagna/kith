"""What the log knows: the quiet dashboard's numbers, and where feeling and fact disagree.

Every number here is derived from `card` and `review` and stored nowhere else, so nothing
can drift out of step with the log. Two definitions worth stating once, in one place,
because the whole page depends on them:

  * **Strength** is FSRS stability mapped onto 0–100. Stability is the gap at which recall
    is expected to fall to 90 %, so 45 days of stability is worth about 63. The curve
    saturates, because the difference between a two-year memory and a five-year one is not
    worth reporting, and it never lies: a card that has never been reviewed is 0.
  * **Known** means strength at least 60 — roughly 41 days of stability, i.e. you could
    name them after a month away and still be right nine times in ten. It is a capability
    claim, not a score, and it can go down.
"""

from __future__ import annotations

import math
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from app.repo import people as people_repo

SOLID_STABILITY = 45.0  # days of stability that count as ~63 strength
KNOWN_STRENGTH = 60  # the "you can name them" line
#: The stability that corresponds to KNOWN_STRENGTH, derived rather than restated, so the
#: two can never fall out of step.
KNOWN_STABILITY = -SOLID_STABILITY * math.log(1.0 - KNOWN_STRENGTH / 100.0)
SPARK_DAYS = 30
RETENTION_DAYS = 30


def strength(stability: float | None) -> int:
    """0–100 from a card's stability. Monotone, saturating, never negative."""
    if not stability or float(stability) <= 0:
        return 0
    value = 100.0 * (1.0 - math.exp(-float(stability) / SOLID_STABILITY))
    return max(0, min(100, int(round(value))))


def _today(now: str | None = None) -> date:
    stamp = now or _now()
    return date.fromisoformat(stamp[:10])


def _now() -> str:
    """The app's own timestamp format, so `due` comparisons line up with what we stored."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def daily_reviews(
    conn: sqlite3.Connection, *, days: int = SPARK_DAYS, now: str | None = None
) -> list[dict]:
    """Answers per day, oldest first, with the empty days kept — a gap is information.

    The day is sliced out of the stored UTC timestamp rather than converted: the log is
    UTC by rule, and a run at 00:30 local belongs to the day it happened on in UTC.
    """
    start = _today(now) - timedelta(days=days - 1)
    rows = conn.execute(
        "SELECT substr(reviewed_at, 1, 10) AS day, count(*) AS answered,"
        " sum(correct) AS correct FROM review"
        " WHERE substr(reviewed_at, 1, 10) >= ? GROUP BY day",
        (start.isoformat(),),
    ).fetchall()
    seen = {str(row["day"]): row for row in rows}
    series = []
    for offset in range(days):
        day = (start + timedelta(days=offset)).isoformat()
        row = seen.get(day)
        series.append(
            {
                "day": day,
                "answered": int(row["answered"]) if row else 0,
                "correct": int(row["correct"] or 0) if row else 0,
            }
        )
    return series


def retention(conn: sqlite3.Connection, *, days: int = RETENTION_DAYS) -> dict:
    """What you actually got right in the window — the honest headline number."""
    series = daily_reviews(conn, days=days)
    answered = sum(day["answered"] for day in series)
    correct = sum(day["correct"] for day in series)
    active_days = sum(1 for day in series if day["answered"])
    return {
        "days": days,
        "answered": answered,
        "correct": correct,
        "rate": (correct / answered) if answered else None,
        "active_days": active_days,
    }


def knowledge(conn: sqlite3.Connection) -> dict:
    """How many people you can name without help, out of how many there are to name."""
    row = conn.execute(
        """
        SELECT
          (SELECT count(*) FROM person p WHERE p.status = 'active'
             AND EXISTS (SELECT 1 FROM photo f WHERE f.person_id = p.id))       AS trainable,
          (SELECT count(*) FROM card c JOIN person p ON p.id = c.person_id
             WHERE c.direction = 'face_to_name' AND c.suspended = 0
               AND p.status = 'active' AND c.reps > 0 AND c.stability >= ?)     AS known,
          (SELECT count(*) FROM card WHERE suspended = 1)                        AS suspended,
          (SELECT count(*) FROM card)                                            AS cards,
          (SELECT count(*) FROM person)                                          AS people
        """,
        (KNOWN_STABILITY,),
    ).fetchone()
    trainable = int(row["trainable"])
    known = int(row["known"])
    return {
        "trainable": trainable,
        "known": known,
        "rate": (known / trainable) if trainable else None,
        "suspended": int(row["suspended"]),
        "cards": int(row["cards"]),
        "people": int(row["people"]),
    }


def difficulty_histogram(conn: sqlite3.Connection) -> list[dict]:
    """How the work spreads over the difficulty spectrum, in five buckets of two.

    Cards that have never been answered carry no difficulty worth reading, so they are
    left out rather than piled up at the default.
    """
    rows = conn.execute(
        "SELECT CAST(min(10, max(1, IFNULL(difficulty, 5))) AS INTEGER) AS d, count(*) AS cards"
        " FROM card WHERE suspended = 0 AND reps > 0 GROUP BY d"
    ).fetchall()
    counts = {int(row["d"]): int(row["cards"]) for row in rows}
    return [
        {
            "low": low,
            "high": low + 1,
            "cards": sum(counts.get(value, 0) for value in range(low, low + 2)),
        }
        for low in range(1, 10, 2)
    ]


def storage(conn: sqlite3.Connection, db_path: str | Path) -> dict:
    """The single-file promise, on the page where you would go to check it."""
    base = Path(str(db_path))
    sizes = {}
    for suffix, key in (("", "db"), ("-wal", "wal"), ("-shm", "shm")):
        path = Path(str(base) + suffix)
        sizes[key] = path.stat().st_size if path.exists() else 0
    row = conn.execute(
        "SELECT count(*) AS photos, IFNULL(sum(bytes), 0) AS photo_bytes FROM photo"
    ).fetchone()
    photos = int(row["photos"])
    photo_bytes = int(row["photo_bytes"])
    return {
        **sizes,
        "total": sum(sizes.values()),
        "photos": photos,
        "photo_bytes": photo_bytes,
        "per_photo": (photo_bytes // photos) if photos else 0,
        "path": str(base),
    }


def group_coverage(conn: sqlite3.Connection, *, weakest: int = 3, now: str | None = None) -> list[dict]:
    """Per group: what share you can name, who is due, and who is letting the side down.

    Only active members count — a paused person is not a gap in your knowledge.
    """
    now = now or _now()
    totals = conn.execute(
        """
        SELECT g.id, g.name,
               count(p.id) AS members,
               sum(CASE WHEN p.id IS NULL THEN 0
                        WHEN c.id IS NULL THEN 1 ELSE 0 END) AS unknown
        FROM grp g
        LEFT JOIN grp_member m ON m.grp_id = g.id
        LEFT JOIN person p ON p.id = m.person_id AND p.status = 'active'
        LEFT JOIN card c ON c.person_id = p.id AND c.direction = 'face_to_name'
             AND c.suspended = 0 AND c.reps > 0 AND c.stability >= ?
        GROUP BY g.id, g.name
        ORDER BY g.name COLLATE NOCASE
        """,
        (KNOWN_STABILITY,),
    ).fetchall()

    ranked = conn.execute(
        f"""
        SELECT * FROM (
          SELECT g.id AS grp_id, p.id AS person_id,
                 {people_repo.RECALL_NAME} AS display_name, p.family_name,
                 IFNULL(c.stability, -1) AS stability,
                 ROW_NUMBER() OVER (
                   PARTITION BY g.id
                   ORDER BY IFNULL(c.stability, -1) ASC, {people_repo.RECALL_NAME} COLLATE NOCASE
                 ) AS rank
          FROM grp g
          JOIN grp_member m ON m.grp_id = g.id
          JOIN person p ON p.id = m.person_id AND p.status = 'active'
          LEFT JOIN card c ON c.person_id = p.id AND c.direction = 'face_to_name'
               AND c.suspended = 0
        ) WHERE rank <= ?
        """,
        (weakest,),
    ).fetchall()

    weakest_by_group: dict[int, list[dict]] = {}
    for row in ranked:
        entry = dict(row)
        entry["strength"] = strength(None if entry["stability"] < 0 else entry["stability"])
        weakest_by_group.setdefault(int(entry["grp_id"]), []).append(entry)

    due_by_group: dict[int, int] = {}
    due_rows = conn.execute(
        "SELECT m.grp_id AS grp_id, count(*) AS due FROM grp_member m"
        " JOIN person p ON p.id = m.person_id AND p.status = 'active'"
        " JOIN card c ON c.person_id = p.id AND c.direction = 'face_to_name'"
        " WHERE c.suspended = 0 AND c.due <= ? GROUP BY m.grp_id",
        (now,),
    ).fetchall()
    for row in due_rows:
        due_by_group[int(row["grp_id"])] = int(row["due"])

    groups = []
    for row in totals:
        members = int(row["members"])
        unknown = int(row["unknown"])
        known = members - unknown
        groups.append(
            {
                "id": int(row["id"]),
                "name": row["name"],
                "members": members,
                "known": known,
                "unknown": unknown,
                "rate": (known / members) if members else None,
                "due": due_by_group.get(int(row["id"]), 0),
                "weakest": weakest_by_group.get(int(row["id"]), []),
            }
        )
    return groups


def calibration(conn: sqlite3.Connection, *, limit: int = 8) -> dict:
    """Where what you felt and what you did disagree.

    `sure_wrong` is the one that earns its place: a mistake you were confident about is the
    most useful thing you can know about your own memory, and the only one that says the
    scheduler's model of you is wrong rather than merely incomplete.
    """
    row = conn.execute(
        """
        SELECT count(confidence)                                          AS rated,
               sum(CASE WHEN confidence >= 2 AND correct = 1 THEN 1 ELSE 0 END) AS sure_right,
               sum(CASE WHEN confidence >= 2 AND correct = 0 THEN 1 ELSE 0 END) AS sure_wrong,
               sum(CASE WHEN confidence = 1 AND correct = 1 THEN 1 ELSE 0 END) AS unsure_right,
               sum(CASE WHEN confidence = 1 AND correct = 0 THEN 1 ELSE 0 END) AS unsure_wrong
        FROM review
        """
    ).fetchone()

    def _rate(hit: int, miss: int) -> float | None:
        total = hit + miss
        return (hit / total) if total else None

    sure_right, sure_wrong = int(row["sure_right"] or 0), int(row["sure_wrong"] or 0)
    unsure_right, unsure_wrong = int(row["unsure_right"] or 0), int(row["unsure_wrong"] or 0)

    worst = conn.execute(
        f"""
        SELECT p.id AS person_id, {people_repo.RECALL_NAME} AS display_name, p.family_name,
               count(*) AS times, max(r.reviewed_at) AS last_at
        FROM review r
        JOIN card c   ON c.id = r.card_id
        JOIN person p ON p.id = c.person_id
        WHERE r.confidence >= 2 AND r.correct = 0
        GROUP BY p.id, p.family_name, {people_repo.RECALL_NAME}
        ORDER BY times DESC, last_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    sure_rate = _rate(sure_right, sure_wrong)
    unsure_rate = _rate(unsure_right, unsure_wrong)
    return {
        "rated": int(row["rated"] or 0),
        "sure_right": sure_right,
        "sure_wrong": sure_wrong,
        "unsure_right": unsure_right,
        "unsure_wrong": unsure_wrong,
        "sure_rate": sure_rate,
        "unsure_rate": unsure_rate,
        # How much more often you are right when you feel sure. Positive is healthy; near
        # zero means your feeling of certainty carries no information about your accuracy.
        "separation": (
            (sure_rate - unsure_rate) if sure_rate is not None and unsure_rate is not None else None
        ),
        "worst": [dict(row) for row in worst],
    }


def person_stats(conn: sqlite3.Connection, person_id: int) -> dict:
    """Everything the detail page can say about one person, in one round of queries."""
    cards = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM card WHERE person_id = ? ORDER BY direction", (person_id,)
        )
    ]
    primary = next((card for card in cards if card["direction"] == "face_to_name"), None)

    agg = conn.execute(
        """
        SELECT count(*) AS answered, IFNULL(sum(correct), 0) AS correct,
               avg(latency_ms) AS mean_latency, max(reviewed_at) AS last_at
        FROM review r JOIN card c ON c.id = r.card_id
        WHERE c.person_id = ? AND r.mode <> 'study'
        """,
        (person_id,),
    ).fetchone()

    recent = conn.execute(
        """
        SELECT correct FROM review r JOIN card c ON c.id = r.card_id
        WHERE c.person_id = ? AND r.mode <> 'study'
        ORDER BY r.reviewed_at DESC LIMIT 40
        """,
        (person_id,),
    ).fetchall()
    flags = [int(row["correct"]) for row in recent]

    def _rate(values: list[int]) -> float | None:
        return (sum(values) / len(values)) if values else None

    # Who this face gets swapped with, in both directions: the name you gave this face, and
    # the faces you gave this person's name to.
    confused = conn.execute(
        f"""
        SELECT {people_repo.recall_name('q')} AS other_name, count(*) AS times
        FROM review r JOIN card c ON c.id = r.card_id
        JOIN person q ON q.id = r.chosen_person_id
        WHERE c.person_id = ? AND r.correct = 0 AND r.chosen_person_id IS NOT NULL
        GROUP BY q.id ORDER BY times DESC LIMIT 5
        """,
        (person_id,),
    ).fetchall()
    mistaken_for = conn.execute(
        f"""
        SELECT {people_repo.RECALL_NAME} AS other_name, count(*) AS times
        FROM review r JOIN card c ON c.id = r.card_id
        JOIN person p ON p.id = c.person_id
        WHERE r.chosen_person_id = ? AND r.correct = 0
        GROUP BY p.id ORDER BY times DESC LIMIT 5
        """,
        (person_id,),
    ).fetchall()

    return {
        "cards": cards,
        "primary": primary,
        "strength": strength(primary["stability"] if primary else None),
        "answered": int(agg["answered"] or 0),
        "correct": int(agg["correct"] or 0),
        "rate": (int(agg["correct"]) / int(agg["answered"])) if int(agg["answered"] or 0) else None,
        "mean_latency_ms": int(agg["mean_latency"]) if agg["mean_latency"] else None,
        "last_at": agg["last_at"],
        "recent_rate": _rate(flags[:20]),
        "earlier_rate": _rate(flags[20:]),
        "confused_with": [dict(row) for row in confused],
        "mistaken_for": [dict(row) for row in mistaken_for],
    }


def overview(
    conn: sqlite3.Connection, *, db_path: str | Path, days: int = RETENTION_DAYS, now: str | None = None
) -> dict:
    """The whole stats page in one dict — the route stays a route."""
    series = daily_reviews(conn, days=SPARK_DAYS, now=now)
    return {
        "series": series,
        "peak": max((day["answered"] for day in series), default=0),
        "retention": retention(conn, days=days),
        "knowledge": knowledge(conn),
        "difficulty": difficulty_histogram(conn),
        "groups": group_coverage(conn),
        "confusion": [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT c.person_id AS person_id, {people_repo.RECALL_NAME} AS display_name,
                       p.family_name, r.chosen_person_id AS other_id,
                       {people_repo.recall_name('q')} AS other_name,
                       count(*) AS mixups
                FROM review r
                JOIN card c   ON c.id = r.card_id
                JOIN person p ON p.id = c.person_id
                JOIN person q ON q.id = r.chosen_person_id
                WHERE r.correct = 0 AND r.chosen_person_id IS NOT NULL
                GROUP BY c.person_id, r.chosen_person_id
                ORDER BY mixups DESC, {people_repo.RECALL_NAME} COLLATE NOCASE
                LIMIT 12
                """
            )
        ],
        "calibration": calibration(conn),
        "storage": storage(conn, db_path),
    }
