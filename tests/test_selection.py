"""The selection engine — *which* card earns the next thirty seconds.

Order is the subject here, so the tests build real FSRS state through the library rather than
writing stability numbers by hand: a hand-written state would let a broken retrievability call
pass unnoticed, which is the one thing this module exists to get right.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest
from fsrs import Rating

from app import config, db, selection, trainer
from app.repo import cards as cards_repo
from app.repo import people as people_repo
from app.repo import sessions as sessions_repo

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
LOOKALIKE = "ffffffffffffffff"
DIFFERENT = "0000000000000000"


def stamp(moment: datetime) -> str:
    return moment.isoformat()


def add_person(conn, name: str) -> int:
    return int(people_repo.create(conn, display_name="", given_name=name))


def reviewed_card(conn, person_id: int, *, rating: Rating, when: datetime) -> int:
    """A card whose state came out of py-fsrs, so its retrievability is real."""
    card_id = int(cards_repo.ensure(conn, person_id)["id"])
    scheduler = cards_repo.make_scheduler()
    card = cards_repo.load(cards_repo.get(conn, card_id))
    card, _log = scheduler.review_card(card, rating, review_datetime=when)
    cards_repo.save_fsrs(conn, card_id, card, now=stamp(when))
    return card_id


def cards_of(conn, card_ids: list[int]) -> list[dict]:
    return [dict(cards_repo.get(conn, card_id)) for card_id in card_ids]


def confuse(conn, *, card_id: int, with_person: int, when: datetime) -> None:
    """Log a real mix-up: asked about this card's person, answered with someone else."""
    conn.execute(
        "INSERT INTO review (card_id, reviewed_at, rating, mode, correct, chosen_person_id)"
        " VALUES (?, ?, 1, 'mc4', 0, ?)",
        (card_id, stamp(when), with_person),
    )
    conn.commit()


# ── the score ────────────────────────────────────────────────────────────────
def test_a_face_you_are_about_to_forget_outranks_one_you_know(settings: config.Settings) -> None:
    """Age is not the signal; the deficit is."""
    with db.connect(settings.db_path) as conn:
        db.migrate(conn)
        shaky = add_person(conn, "Shaky")
        solid = add_person(conn, "Solid")
        shaky_card = reviewed_card(conn, shaky, rating=Rating.Again, when=NOW - timedelta(days=30))
        solid_card = reviewed_card(conn, solid, rating=Rating.Easy, when=NOW - timedelta(days=1))
        scored = selection.score_candidates(
            conn, cards_of(conn, [shaky_card, solid_card]), now=stamp(NOW)
        )

    by_card = {candidate.card_id: candidate for candidate in scored}
    assert by_card[shaky_card].retrievability < by_card[solid_card].retrievability
    assert by_card[shaky_card].score > by_card[solid_card].score
    assert by_card[shaky_card].overdue > by_card[solid_card].overdue

    # and the order it produces reflects that, most of the time but not always
    firsts = [
        selection.pick(scored, 2, rng=random.Random(seed))[0].card_id for seed in range(40)
    ]
    assert firsts.count(shaky_card) > firsts.count(solid_card)


def test_mixing_two_people_up_raises_both_of_them(settings: config.Settings) -> None:
    """The distractor matters as much as the target: you reached for that name for a reason."""
    with db.connect(settings.db_path) as conn:
        db.migrate(conn)
        asked = add_person(conn, "Asked")
        reached_for = add_person(conn, "ReachedFor")
        innocent = add_person(conn, "Innocent")
        ids = {
            person: reviewed_card(conn, person, rating=Rating.Good, when=NOW - timedelta(days=3))
            for person in (asked, reached_for, innocent)
        }
        confuse(conn, card_id=ids[asked], with_person=reached_for, when=NOW - timedelta(days=2))

        counts = selection.confusion_counts(conn)
        scored = {
            candidate.person_id: candidate
            for candidate in selection.score_candidates(
                conn, cards_of(conn, list(ids.values())), now=stamp(NOW)
            )
        }

    assert counts.get(asked) == 1 and counts.get(reached_for) == 1
    assert counts.get(innocent) is None
    # same card state, same due date — the only difference is the mix-up
    assert scored[asked].score > scored[innocent].score
    assert scored[reached_for].score > scored[innocent].score


def test_a_card_never_reviewed_is_treated_as_fully_at_risk(settings: config.Settings) -> None:
    """The library has nothing to say about an unreviewed card; that must not crash the queue."""
    with db.connect(settings.db_path) as conn:
        db.migrate(conn)
        person = add_person(conn, "Unseen")
        card_id = int(cards_repo.ensure(conn, person)["id"])
        scored = selection.score_candidates(conn, cards_of(conn, [card_id]), now=stamp(NOW))

    assert scored[0].retrievability == 0.0
    assert scored[0].score > 0


# ── the pick ─────────────────────────────────────────────────────────────────
def test_the_pick_never_repeats_a_card_or_invents_one(settings: config.Settings) -> None:
    with db.connect(settings.db_path) as conn:
        db.migrate(conn)
        card_ids = [
            reviewed_card(
                conn, add_person(conn, f"Person {index}"), rating=Rating.Good,
                when=NOW - timedelta(days=index),
            )
            for index in range(6)
        ]
        scored = selection.score_candidates(conn, cards_of(conn, card_ids), now=stamp(NOW))

    chosen = selection.pick(scored, len(scored), rng=random.Random(1))
    picked = [candidate.card_id for candidate in chosen]
    assert len(picked) == len(set(picked)) == len(card_ids)
    assert set(picked) == set(card_ids)


def test_the_order_is_sampled_rather_than_taken_from_the_top(settings: config.Settings) -> None:
    """Taking the maximum every time would hand the learner the same order every session."""
    with db.connect(settings.db_path) as conn:
        db.migrate(conn)
        card_ids = [
            reviewed_card(
                conn, add_person(conn, f"Same {index}"), rating=Rating.Good,
                when=NOW - timedelta(days=2),
            )
            for index in range(3)
        ]
        scored = selection.score_candidates(conn, cards_of(conn, card_ids), now=stamp(NOW))

    firsts = {
        selection.pick(scored, 3, rng=random.Random(seed))[0].card_id for seed in range(60)
    }
    assert len(firsts) >= 2, "three identical cards must not always produce the same first item"


def test_the_maintenance_floor_spends_its_slots_on_what_the_model_thinks_is_known(
    settings: config.Settings,
) -> None:
    """Otherwise the trainer only ever asks what it already predicts you will forget."""
    with db.connect(settings.db_path) as conn:
        db.migrate(conn)
        card_ids = [
            reviewed_card(
                conn, add_person(conn, f"Known {index}"), rating=Rating.Easy,
                when=NOW - timedelta(days=1),
            )
            for index in range(20)
        ]
        scored = selection.score_candidates(conn, cards_of(conn, card_ids), now=stamp(NOW))

    chosen = selection.pick(scored, 20, rng=random.Random(2), maintenance_floor=0.10)
    maintenance = [candidate for candidate in chosen if candidate.exploration]
    assert len(maintenance) == 2, "10 % of a twenty-item session is two maintenance draws"
    assert all(candidate.retrievability >= selection.DEFAULT_RETENTION for candidate in maintenance)

    small = selection.pick(scored, 10, rng=random.Random(2), maintenance_floor=0.10)
    assert sum(1 for candidate in small if candidate.exploration) == 1


def test_the_floor_reaches_into_the_tail_of_a_short_session(settings: config.Settings) -> None:
    """A session with room for ten of thirty: without the floor all ten come from the head."""
    with db.connect(settings.db_path) as conn:
        db.migrate(conn)
        card_ids = [
            reviewed_card(
                conn, add_person(conn, f"Tail {index}"), rating=Rating.Easy,
                when=NOW - timedelta(days=1),
            )
            for index in range(30)
        ]
        scored = selection.score_candidates(conn, cards_of(conn, card_ids), now=stamp(NOW))

        chosen = selection.pick(scored, 10, rng=random.Random(9), maintenance_floor=0.10)
        # the same seed and the same budget without the floor: this is what scoring alone wants
        scored_only = {
            candidate.card_id
            for candidate in selection.pick(scored, 9, rng=random.Random(9), maintenance_floor=0.0)
        }

    maintenance = [candidate for candidate in chosen if candidate.exploration]
    assert len(chosen) == 10 and len(maintenance) == 1
    assert maintenance[0].retrievability >= selection.DEFAULT_RETENTION
    assert maintenance[0].card_id not in scored_only, "the floor must reach past the score"


def test_the_maintenance_draws_are_spread_through_the_session(settings: config.Settings) -> None:
    with db.connect(settings.db_path) as conn:
        db.migrate(conn)
        card_ids = [
            reviewed_card(
                conn, add_person(conn, f"Spread {index}"), rating=Rating.Easy,
                when=NOW - timedelta(days=1),
            )
            for index in range(30)
        ]
        scored = selection.score_candidates(conn, cards_of(conn, card_ids), now=stamp(NOW))

    chosen = selection.pick(scored, 30, rng=random.Random(5), maintenance_floor=0.10)
    spots = [index for index, candidate in enumerate(chosen) if candidate.exploration]
    assert len(spots) == 3
    assert spots[0] > 0, "a maintenance card must not open the session"
    assert spots[-1] < len(chosen) - 1, "nor close it"


# ── the spread ───────────────────────────────────────────────────────────────
def test_lookalike_faces_are_not_asked_back_to_back(settings: config.Settings) -> None:
    """Two nearly identical photos are confusable as images, so asking them in a row is noise."""
    with db.connect(settings.db_path) as conn:
        db.migrate(conn)
        twin_a = add_person(conn, "Twin A")
        twin_b = add_person(conn, "Twin B")
        other = add_person(conn, "Other")
        ids = [
            reviewed_card(conn, person, rating=Rating.Good, when=NOW - timedelta(days=2))
            for person in (twin_a, twin_b, other)
        ]
        scored = selection.score_candidates(conn, cards_of(conn, ids), now=stamp(NOW))

    chosen = selection.pick(scored, 3, rng=random.Random(3))
    spread = selection.spread_apart(
        chosen,
        hashes={twin_a: LOOKALIKE, twin_b: LOOKALIKE, other: DIFFERENT},
        threshold=4,
    )
    order = [candidate.person_id for candidate in spread]
    assert len(order) == 3
    assert abs(order.index(twin_a) - order.index(twin_b)) > 1, order


def test_a_queue_of_lookalikes_still_runs_in_full(settings: config.Settings) -> None:
    """When everything looks alike the wait is honoured as far as it can be, never by dropping."""
    with db.connect(settings.db_path) as conn:
        db.migrate(conn)
        people = [add_person(conn, f"Clone {index}") for index in range(4)]
        ids = [
            reviewed_card(conn, person, rating=Rating.Good, when=NOW - timedelta(days=2))
            for person in people
        ]
        scored = selection.score_candidates(conn, cards_of(conn, ids), now=stamp(NOW))

    chosen = selection.pick(scored, 4, rng=random.Random(4))
    spread = selection.spread_apart(
        chosen, hashes={person: LOOKALIKE for person in people}, threshold=4
    )
    assert sorted(candidate.card_id for candidate in spread) == sorted(
        candidate.card_id for candidate in chosen
    )


# ── the wiring ───────────────────────────────────────────────────────────────
def test_a_session_opens_with_the_face_you_are_about_to_forget(
    settings: config.Settings,
) -> None:
    """The engine is not decoration: it decides the queue, and here it disagrees with `due`.

    `Known` is the *most* overdue card in the fixture — reviewed a month ago, but reviewed on
    `Easy`, so its interval grew long enough that a month late still finds it half-remembered.
    Sorting by `due` would open with it. `Shaky` failed ten days ago and is at the point of
    being lost, which is the card worth the next thirty seconds.
    """
    with db.connect(settings.db_path) as conn:
        db.migrate(conn)
        shaky = add_person(conn, "Shaky")
        known = add_person(conn, "Known")
        shaky_card = reviewed_card(conn, shaky, rating=Rating.Again, when=NOW - timedelta(days=10))
        known_card = reviewed_card(conn, known, rating=Rating.Easy, when=NOW - timedelta(days=30))
        due = {
            int(row["id"]): str(row["due"])
            for row in conn.execute(
                "SELECT id, due FROM card WHERE id IN (?, ?)", (shaky_card, known_card)
            )
        }
        session_id = trainer.build_session(
            conn,
            mode_keys=["study", "recall_self"],
            new_budget=0,
            minutes=10,
            include_all=True,
            rng=random.Random(7),
            now=stamp(NOW),
        )
        order = [
            int(row["card_id"])
            for row in conn.execute(
                "SELECT card_id FROM session_item WHERE session_id = ? ORDER BY position, id",
                (session_id,),
            )
        ]
        scores = {
            candidate.card_id: candidate.score
            for candidate in selection.score_candidates(
                conn, cards_of(conn, [shaky_card, known_card]), now=stamp(NOW)
            )
        }

    assert due[known_card] < due[shaky_card], "fixture must contradict due-order"
    assert scores[shaky_card] > scores[known_card], "and the score must contradict it too"
    assert set(order) == {shaky_card, known_card}
    assert order[0] == shaky_card, f"due-order would open with {due}; the engine opens with the deficit"
