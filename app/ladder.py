"""The ladder: which of the modes you ticked to ask, for this card, right now.

Two skills, two ladders. A card already knows its direction, so its tiers are simply the ticked
modes of that direction; what this module decides is *where on them the card currently sits*.

The rule is one rung at a time, read from the review log:

  * never asked, or the last answer was a miss → the gentlest tier. A miss in a harder tier
    drops the card back to the tier *below* it, so a demotion is as visible as a promotion.
  * last answer right, and the card is settled → one rung up.
  * otherwise → the same tier again.

One rung, and never "the hardest mode allowed". A face you always recognise has demonstrated
nothing about *producing* a name, and jumping straight to free recall is how a learner ends up
being asked something they never ticked. Tiers are only ever modes that were actually ticked, so
ticking one mode means being asked in that mode and no other, for as long as you keep ticking it
— the ladder has nowhere else to go.
"""

from __future__ import annotations

import random
import sqlite3

from app.modes.base import Mode

SETTLED_REPS = 2          # one correct answer is a data point; two is the start of a habit


def tiers_for(modes: list[Mode], direction: str) -> list[Mode]:
    """The ticked, gradeable modes of one direction, gentlest first."""
    return sorted(
        (mode for mode in modes if mode.gradeable and mode.direction == direction),
        key=lambda mode: (mode.demand, mode.key),
    )


def direction_of(mode: Mode | None) -> str:
    """The card direction a mode is scheduled on.

    `face_to_name` for anything without a direction of its own (a study card): the primary axis
    is where a face is introduced, and a card must never be graded into the wrong one.
    """
    direction = str(getattr(mode, "direction", "") or "")
    return direction if direction in ("face_to_name", "name_to_face") else "face_to_name"


def choose(
    tiers: list[Mode],
    *,
    last: dict | None,
    reps: int,
    lapses: int,
) -> Mode | None:
    """Where this card sits on the ladder it is allowed to climb."""
    if not tiers:
        return None
    if last is None:
        return tiers[0]                       # never asked: the gentlest way you allowed

    asked = _index_of(tiers, str(last.get("mode") or ""))
    if not bool(last.get("correct")) or last.get("no_idea"):
        # A miss drops one rung — including the miss you just made in a harder mode.
        return tiers[max(0, asked - 1)] if asked is not None else tiers[0]

    if asked is None:
        return tiers[0]                       # answered in a mode that is no longer ticked
    if int(reps) >= SETTLED_REPS and int(lapses) == 0 and asked + 1 < len(tiers):
        return tiers[asked + 1]               # one rung up, and no further
    return tiers[asked]


def _index_of(tiers: list[Mode], key: str) -> int | None:
    for index, mode in enumerate(tiers):
        if mode.key == key:
            return index
    return None


def last_answers(conn: sqlite3.Connection) -> dict[int, dict]:
    """Every card's most recent answer, in one query — the ladder's whole evidence base.

    The review log is append-only and already records the mode of every answer, so the ladder
    needs no column of its own and no migration: promotion is derived, not stored.
    """
    rows = conn.execute(
        """
        SELECT r.card_id, r.mode, r.correct, r.no_idea
        FROM review r
        JOIN (SELECT card_id, MAX(id) AS last_id FROM review GROUP BY card_id) m
          ON m.last_id = r.id
        """
    ).fetchall()
    return {
        int(row["card_id"]): {
            "mode": str(row["mode"]),
            "correct": bool(row["correct"]),
            "no_idea": bool(row["no_idea"]),
        }
        for row in rows
    }
