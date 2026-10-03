"""The calibration tap: does it survive the round trip into the log?

Every other number on the Progress page can be recomputed from columns the trainer already
writes. Confidence is the exception — it exists only if the client asks for it, so a capture
that quietly dropped would leave the calibration looking honest while being empty.
"""

from __future__ import annotations

import random
import uuid

from app import db, trainer
from app.repo import cards as cards_repo
from app.repo import sessions as sessions_repo


def _people(conn, names: list[str]) -> None:
    """Photographed people with cards: enough of them for a multiple-choice question."""
    for index, name in enumerate(names):
        now = db.utcnow()
        cursor = conn.execute(
            "INSERT INTO person (display_name, given_name, status, created_at, updated_at)"
            " VALUES (?, ?, 'active', ?, ?)",
            (name, name, now, now),
        )
        person_id = int(cursor.lastrowid or 0)
        conn.execute(
            "INSERT INTO photo (person_id, is_primary, mime, width, height, bytes, sha256,"
            " data, thumb, created_at) VALUES (?, 1, 'image/webp', 8, 8, 64, ?, ?, ?, ?)",
            (person_id, f"sha-{index}-{name}-{uuid.uuid4().hex[:6]}", b"", b"", now),
        )
        # A card, or the question could not be multiple choice: a face never seen before is
        # introduced with a study card instead, whatever modes are ticked.
        cards_repo.ensure(conn, person_id, "face_to_name")


def _answered(settings, extra: dict | None = None) -> dict:
    """Answer one multiple-choice question and return the row the log recorded."""
    with db.connect(settings.db_path) as conn:
        db.migrate(conn)
        _people(conn, ["Anna", "Bahar", "Clara", "Daan"])
        session_id = trainer.build_session(
            conn,
            mode_keys=["mc4"],
            new_budget=0,
            minutes=10,
            include_all=True,
            rng=random.Random(3),
            now=db.utcnow(),
        )
        sheet = sessions_repo.get(conn, session_id)
        assert sheet is not None
        item = next(i for i in sessions_repo.items(conn, session_id) if i["mode"] == "mc4")
        target = trainer.target_person(conn, item)
        assert target is not None
        answer = {"item_id": int(item["id"]), "person_id": int(target["id"]), "latency_ms": 900}
        answer.update(extra or {})
        trainer.submit(conn, sheet, item, answer)
        row = conn.execute(
            "SELECT confidence, correct FROM review WHERE session_item_id = ?",
            (int(item["id"]),),
        ).fetchone()
        return dict(row) if row else {}


def test_a_tapped_confidence_is_stored(settings):
    row = _answered(settings, {"confidence": 2})

    assert row["confidence"] == 2, row
    assert row["correct"] == 1, row


def test_saying_nothing_stores_nothing_rather_than_a_zero(settings):
    """NULL means "did not say"; a 0 would be averaged in as if you had rated yourself."""
    assert _answered(settings)["confidence"] is None


def test_nonsense_is_dropped_rather_than_guessed(settings):
    for nonsense in ("sure", 9, -1, 0):
        assert _answered(settings, {"confidence": nonsense})["confidence"] is None, nonsense
