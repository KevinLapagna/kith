"""Photos: the only place in the app that touches image blobs."""

from __future__ import annotations

import sqlite3

from app.db import utcnow
from app.images import Imported, hamming

# Metadata only. Naming the columns is not pedantry: `SELECT *` here would pull two
# WebP blobs per row into every list page.
_META = "id, person_id, is_primary, mime, width, height, bytes, sha256, dhash, crop, source, caption, created_at"


def find_by_sha256(conn: sqlite3.Connection, sha256: str) -> dict | None:
    """Exact-duplicate check — re-uploading the same file is a no-op, not a second entry."""
    row = conn.execute(
        f"SELECT {_META} FROM photo WHERE sha256 = ?", (sha256,)
    ).fetchone()
    return dict(row) if row else None


def add(
    conn: sqlite3.Connection,
    person_id: int,
    image: Imported,
    *,
    source: str | None = None,
    caption: str | None = None,
    make_primary: bool = False,
) -> int:
    """Insert a prepared image. The first photo of a person becomes the primary one."""
    existing = conn.execute(
        "SELECT count(*) FROM photo WHERE person_id = ?", (person_id,)
    ).fetchone()[0]
    is_primary = 1 if (make_primary or existing == 0) else 0

    conn.execute("SAVEPOINT add_photo")
    try:
        if is_primary:
            conn.execute("UPDATE photo SET is_primary = 0 WHERE person_id = ?", (person_id,))
        cursor = conn.execute(
            """
            INSERT INTO photo (person_id, is_primary, mime, width, height, bytes, sha256,
                               dhash, crop, source, caption, data, thumb, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)
            """,
            (
                person_id,
                is_primary,
                image.mime,
                image.width,
                image.height,
                image.bytes,
                image.sha256,
                image.dhash,
                source,
                caption,
                image.data,
                image.thumb,
                utcnow(),
            ),
        )
        conn.execute("RELEASE add_photo")
    except sqlite3.Error:
        conn.execute("ROLLBACK TO add_photo")
        conn.execute("RELEASE add_photo")
        raise
    return int(cursor.lastrowid or 0)


def listing(conn: sqlite3.Connection, person_id: int) -> list[dict]:
    rows = conn.execute(
        f"SELECT {_META} FROM photo WHERE person_id = ? ORDER BY is_primary DESC, id",
        (person_id,),
    )
    return [dict(row) for row in rows]


def get_meta(conn: sqlite3.Connection, photo_id: int) -> dict | None:
    row = conn.execute(f"SELECT {_META} FROM photo WHERE id = ?", (photo_id,)).fetchone()
    return dict(row) if row else None


def get_blob(conn: sqlite3.Connection, photo_id: int, *, thumb: bool = False) -> tuple[bytes, str] | None:
    """The one query that does select a blob, and never more than one row."""
    column = "thumb" if thumb else "data"
    row = conn.execute(
        f"SELECT {column} AS blob, mime FROM photo WHERE id = ?", (photo_id,)
    ).fetchone()
    if row is None or row["blob"] is None:
        return None
    return bytes(row["blob"]), str(row["mime"])


def thumbs_for(conn: sqlite3.Connection, person_ids: list[int]) -> dict[int, bytes]:
    """Primary (or first) thumbnail per person, in a single query — no N+1.

    Returns raw bytes; the caller streams them as data URLs or via the thumbnail
    endpoint. Kept as a dict so a missing photo is simply an absent key.
    """
    if not person_ids:
        return {}
    placeholders = ", ".join("?" for _ in person_ids)
    rows = conn.execute(
        f"""
        SELECT person_id, thumb FROM (
          SELECT person_id, thumb,
                 ROW_NUMBER() OVER (PARTITION BY person_id
                                    ORDER BY is_primary DESC, id) AS rank
          FROM photo WHERE person_id IN ({placeholders})
        ) WHERE rank = 1
        """,
        person_ids,
    )
    return {int(row["person_id"]): bytes(row["thumb"]) for row in rows}


def set_primary(conn: sqlite3.Connection, person_id: int, photo_id: int) -> bool:
    conn.execute("SAVEPOINT set_primary")
    try:
        conn.execute("UPDATE photo SET is_primary = 0 WHERE person_id = ?", (person_id,))
        changed = conn.execute(
            "UPDATE photo SET is_primary = 1 WHERE id = ? AND person_id = ?",
            (photo_id, person_id),
        ).rowcount
        conn.execute("RELEASE set_primary")
    except sqlite3.Error:
        conn.execute("ROLLBACK TO set_primary")
        conn.execute("RELEASE set_primary")
        raise
    return changed > 0


def delete(conn: sqlite3.Connection, photo_id: int) -> bool:
    """Deleting the primary photo promotes the next one, so a person never ends up
    with photos but no thumbnail."""
    row = conn.execute(
        "SELECT person_id, is_primary FROM photo WHERE id = ?", (photo_id,)
    ).fetchone()
    if row is None:
        return False
    conn.execute("SAVEPOINT del_photo")
    try:
        conn.execute("DELETE FROM photo WHERE id = ?", (photo_id,))
        if row["is_primary"]:
            conn.execute(
                "UPDATE photo SET is_primary = 1 WHERE id = "
                "(SELECT id FROM photo WHERE person_id = ? ORDER BY id LIMIT 1)",
                (row["person_id"],),
            )
        conn.execute("RELEASE del_photo")
    except sqlite3.Error:
        conn.execute("ROLLBACK TO del_photo")
        conn.execute("RELEASE del_photo")
        raise
    return True


def similar(
    conn: sqlite3.Connection,
    dhash: str,
    *,
    exclude_person: int | None = None,
    max_distance: int = 14,
    limit: int = 12,
) -> list[dict]:
    """Visually similar photos, nearest first.

    This is the seed of the trainer's distractor engine (M3): a wrong answer that looks
    like the right one makes you retrieve the distinguishing feature, while a wrong
    answer that looks nothing like it barely tests anything. A Hamming distance over a
    64-bit difference hash is crude next to a real face embedding, but it needs no new
    dependency — and at a few thousand photos the whole scan is still trivial.
    """
    rows = conn.execute(
        "SELECT id, person_id, dhash FROM photo WHERE dhash IS NOT NULL AND id > 0"
    )
    scored = []
    for row in rows:
        if exclude_person is not None and row["person_id"] == exclude_person:
            continue
        distance = hamming(dhash, row["dhash"])
        if distance <= max_distance:
            scored.append((distance, dict(row)))
    scored.sort(key=lambda pair: (pair[0], pair[1]["id"]))
    return [item for _, item in scored[:limit]]
