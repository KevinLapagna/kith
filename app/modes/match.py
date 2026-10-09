"""Match 3 ↔ 3 and Match 6 ↔ 6 — several faces and several names at once, paired up.

The board is the *cluster* the item is about: the target the ladder chose, plus companions picked
by the same rule the multiple-choice distractors use (same group first, then faces that look
alike). That is what makes a board a discrimination test rather than a memory test — and it is
also why a board is worth grading for every member, not only for its target.

The interaction is a pairing, so the answer is a pairing: one placement per face, and every face
carries exactly one name before the board can be graded. That shape buys something no
single-target mode can: a mismatch names *both* people in one gesture ("Anna's name on Bahar's
face"), which is exactly the confusion the drill in §9.2 lives on.

Tap-tap is always available alongside drag (§6). The server never learns which gesture put a name
on a face, and it does not need to: `matching` is a plain mapping of face → name.
"""

from __future__ import annotations

import random

from app.modes.base import BuildContext, Mode, Outcome, person_photo_url, pick_distractors


def roster_for(
    pool: list[dict],
    target: dict,
    size: int,
    rng: random.Random,
    *,
    recent_person_ids: list[int] | None = None,
) -> list[dict]:
    """The board's people: the target first, then companions chosen to be confusable.

    Deliberately the same preference order as the multiple-choice distractors — people from the
    same group, then lookalikes. A board of strangers would test nothing.
    """
    ctx = BuildContext(
        target=target,
        pool=pool,
        rng=rng,
        recent_person_ids=list(recent_person_ids or []),
    )
    return [target, *pick_distractors(ctx, max(0, int(size) - 1))]


class MatchBase(Mode):
    """Shared behaviour of both board sizes."""

    direction = "face_to_name"     # the names are given; the faces are the question
    input = "drag"                 # drag, or tap-tap — both are offered, neither is required
    board = True                   # one item, several people: graded by `grade_board`
    hintable = False               # nothing to hint at: every name is already on the board
    prompt_key = "mode.match.prompt"

    def build(self, ctx: BuildContext) -> dict:
        roster = list(ctx.board)
        faces = [
            {"person_id": int(person["id"]), "photo_url": person_photo_url(person)}
            for person in roster
        ]
        names = [
            {"person_id": int(person["id"]), "label": person["display_name"]}
            for person in roster
        ]
        # Shuffled independently: two lists in the same order would hand the answer over, and
        # the point of a board is that every face has to be looked at on its own.
        ctx.rng.shuffle(faces)
        ctx.rng.shuffle(names)
        return {
            "mode": self.key,
            "direction": self.direction,
            "prompt": self.prompt,
            "board": True,
            "faces": faces,
            "names": names,
            "hintable": False,
        }

    def grade(self, target: dict, payload: dict, answer: dict) -> Outcome:
        raise ValueError("a board is graded pair by pair, not as a single answer")

    def grade_board(self, roster: list[dict], payload: dict, answer: dict) -> list[dict]:
        """One result per board member: right, or the name they were given instead.

        Refuses rather than inventing. A partial board, a face that was never shown, or an answer
        that is not a mapping at all is a refusal — the same way an empty multiple-choice tap is —
        because a guess dressed up as a verdict would go straight into the review log.
        """
        matching = answer.get("matching")
        if not isinstance(matching, dict) or not matching:
            raise ValueError("a match answer maps each face to the name you put on it")
        on_board = {int(face["person_id"]) for face in payload.get("faces", [])}
        if not on_board:
            raise ValueError("that board is no longer on screen")
        placements: dict[int, int] = {}
        for raw_face, raw_name in matching.items():
            try:
                face_id, name_id = int(raw_face), int(raw_name)
            except (TypeError, ValueError):
                raise ValueError("a match answer maps person ids to person ids") from None
            if face_id not in on_board:
                raise ValueError("a name was placed on a face that was not on the board")
            placements[face_id] = name_id
        if len(placements) != len(on_board):
            raise ValueError("every face on the board needs a name before it can be graded")

        results: list[dict] = []
        for entry in roster:
            person_id = int(entry["person_id"])
            chosen = placements.get(person_id)
            results.append(
                {
                    "person_id": person_id,
                    "card_id": entry.get("card_id"),
                    "chosen_person_id": chosen,
                    # A misplaced name is a miss for the face it landed on *and* for the person
                    # whose name was moved: one gesture, two people confused, both recorded.
                    "correct": chosen == person_id,
                }
            )
        return results


class Match3(MatchBase):
    key = "match3"
    label_key = "mode.match3.label"
    demand = 3
    min_people = 3                 # the target plus two companions
    board_size = 3


class Match6(MatchBase):
    key = "match6"
    label_key = "mode.match6.label"
    demand = 4
    min_people = 6
    board_size = 6
