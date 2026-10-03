"""Match 3 ↔ 3 and Match 6 ↔ 6: one queue item, several people, graded pair by pair.

Like the trainer's own tests, these drive `trainer.build_session` and `trainer.submit` — the same
calls the routes make — so a green suite means the real loop ran.
"""

from __future__ import annotations

import random

import pytest

from app import db, selection, trainer
from app.repo import cards as cards_repo
from app.repo import people as people_repo
from app.repo import sessions as sessions_repo

NAMES = [
    "Anna Brunner",
    "Bahar Demir",
    "Clara Fontaine",
    "Diego Alvarez",
    "Elena Petrova",
    "Faisal Khan",
]


def cabinet(settings, names=NAMES) -> list[int]:
    """People, and nothing else. Cards are created when a test asks for them."""
    with db.connect(settings.db_path) as conn:
        db.migrate(conn)
        return [int(people_repo.create(conn, display_name=name)) for name in names]


def trainable(settings, ids: list[int]) -> None:
    """Face→name cards, so those faces can be scheduled — the rest are companions."""
    with db.connect(settings.db_path) as conn:
        for person_id in ids:
            cards_repo.ensure(conn, person_id)


def a_board(settings, mode: str = "match3") -> tuple[int, dict, dict]:
    """The shape every test here starts from: a session with one board on it."""
    with db.connect(settings.db_path) as conn:
        session_id = trainer.build_session(
            conn,
            mode_keys=[mode],
            new_budget=0,
            minutes=10,
            include_all=True,
            rng=random.Random(4),
            now=db.utcnow(),
        )
        session = sessions_repo.get(conn, session_id)
        item = next(
            item
            for item in sessions_repo.items(conn, session_id)
            if str(item["mode"]) == mode
        )
        payload = trainer.payload_for(conn, session, item)
    return session_id, item, payload


def placed(payload: dict) -> dict[str, int]:
    """Every face wearing its own name — the board as it looks when it is perfect."""
    return {str(face["person_id"]): int(face["person_id"]) for face in payload["faces"]}


def submit_board(settings, session_id: int, item: dict, answer: dict) -> dict:
    with db.connect(settings.db_path) as conn:
        session = sessions_repo.get(conn, session_id)
        return trainer.submit(conn, session, item, answer)["verdict"]


def reviews_for(settings, item_id: int) -> list[dict]:
    with db.connect(settings.db_path) as conn:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT card_id, correct, rating, chosen_person_id FROM review"
                " WHERE session_item_id = ?",
                (item_id,),
            )
        ]


# ── the board is the roster, and the roster is the board ─────────────────────
def test_a_board_is_exactly_the_roster_that_was_stored(settings):
    """The board cannot be re-picked at answer time, so it is written into the item."""
    ids = cabinet(settings)
    trainable(settings, ids)
    _session_id, item, payload = a_board(settings)

    roster = [int(entry["person_id"]) for entry in item["people"]]
    assert len(roster) == 3, roster
    assert item["people"][0]["card_id"] == item["card_id"], "the ladder's target leads the board"
    assert len(set(roster)) == 3, "a board never shows the same face twice"

    assert payload["board"] is True
    assert {int(face["person_id"]) for face in payload["faces"]} == set(roster)
    assert {int(name["person_id"]) for name in payload["names"]} == set(roster)
    assert all("label" not in face for face in payload["faces"]), "a caption is the answer key"
    assert all(name["label"] for name in payload["names"])


def test_six_people_make_a_six_board(settings):
    ids = cabinet(settings)
    trainable(settings, ids)
    _session_id, item, payload = a_board(settings, mode="match6")

    assert len(item["people"]) == 6
    assert len(payload["faces"]) == 6
    assert len(payload["names"]) == 6
    assert {int(face["person_id"]) for face in payload["faces"]} == {
        int(entry["person_id"]) for entry in item["people"]
    }


# ── grading: one item, one review per member ─────────────────────────────────
def test_a_perfect_board_schedules_everyone_on_it(settings):
    ids = cabinet(settings)
    trainable(settings, ids)
    session_id, item, payload = a_board(settings)

    verdict = submit_board(settings, session_id, item, {"matching": placed(payload)})
    rows = reviews_for(settings, int(item["id"]))

    assert verdict["board"] is True and verdict["correct"] is True
    assert [result["correct"] for result in verdict["results"]] == [True, True, True]
    assert len(rows) == 3, "one review per board member, not one per item"
    assert len({row["card_id"] for row in rows}) == 3, "each member has their own card"
    assert all(row["correct"] == 1 and row["rating"] == 3 for row in rows)


def test_a_swapped_name_is_recorded_as_a_confusion_both_ways(settings):
    """One misplaced name confuses two people at once, and both halves are worth keeping.

    This is the reason a board is graded for every member rather than only for its target: the
    slip ("Anna's name on Bahar's face") is the same signal the confusion drill is built from,
    and it would be invisible if only the target's card were written down.
    """
    ids = cabinet(settings)
    trainable(settings, ids)
    session_id, item, payload = a_board(settings)
    first, second = (int(face["person_id"]) for face in payload["faces"][:2])
    answer = placed(payload)
    answer[str(first)], answer[str(second)] = second, first

    verdict = submit_board(settings, session_id, item, {"matching": answer})
    rows = reviews_for(settings, int(item["id"]))
    results = {int(result["person_id"]): result for result in verdict["results"]}

    assert verdict["correct"] is False
    assert results[first]["chosen_person_id"] == second
    assert results[second]["chosen_person_id"] == first
    assert sum(row["correct"] for row in rows) == 1, "the untouched face stays right"

    with db.connect(settings.db_path) as conn:
        # Who was mistaken for whom, not merely how often: the pairing is what the drill wants.
        mismatches = trainer.confusion_map(conn, trainer.training_pool(conn))
        counted = selection.confusion_counts(conn)
        still_open = sessions_repo.item(conn, int(item["id"]))
    assert second in mismatches.get(first, ()), mismatches
    assert first in mismatches.get(second, ()), mismatches
    assert counted.get(first, 0) >= 1 and counted.get(second, 0) >= 1, counted
    assert still_open["state"] != "answered", "a board worth repeating comes back"


def test_a_partial_board_is_refused_rather_than_guessed(settings):
    ids = cabinet(settings)
    trainable(settings, ids)
    session_id, item, payload = a_board(settings)
    first = int(payload["faces"][0]["person_id"])

    with db.connect(settings.db_path) as conn:
        session = sessions_repo.get(conn, session_id)
        with pytest.raises(ValueError):
            trainer.submit(conn, session, item, {"matching": {str(first): first}})
        assert conn.execute("SELECT count(*) FROM review").fetchone()[0] == 0, (
            "a guess dressed up as a verdict must not reach the review log"
        )


def test_a_companion_without_a_card_rides_along_without_being_scheduled(settings):
    """A face with nothing to schedule is a good face to tell apart and nobody to write down.

    Creating a card for it here would slip a face into the rotation that the learner was never
    introduced to — the one thing a board must not do.
    """
    ids = cabinet(settings)
    trainable(settings, ids[:1])          # one trainable person; the rest are only faces
    session_id, item, payload = a_board(settings)

    verdict = submit_board(settings, session_id, item, {"matching": placed(payload)})
    rows = reviews_for(settings, int(item["id"]))

    with db.connect(settings.db_path) as conn:
        cards = conn.execute("SELECT count(*) FROM card").fetchone()[0]

    assert verdict["correct"] is True
    assert sum(1 for result in verdict["results"] if result["scheduled"]) == 1
    assert len(rows) == 1, "only the trainable member is written down"
    assert cards == 1, "and no card was created for the others"
