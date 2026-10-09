"""Reverse: four faces, one name — the same recognition skill as multiple choice, backwards.

Recognition and production are separate skills, which is why a person carries two cards; this
mode is why the second one exists. The name is the question and the face is the answer, so the
options are photographs and they deliberately carry **no name**: the whole point is to know
which face goes with the name you were just given. The name arrives again with the verdict.

It gets its own card direction so the scheduler never averages the two skills — a face you
always recognise is not a name you can produce.
"""

from __future__ import annotations

from app.modes.base import (
    BuildContext,
    Mode,
    Outcome,
    face_options,
    grade_choice,
    pick_distractors,
)


class Reverse(Mode):
    key = "reverse"
    label_key = "mode.reverse.label"
    prompt_key = "mode.reverse.prompt"
    direction = "name_to_face"
    demand = 3
    min_people = 4        # target + three distractor faces
    input = "tap"
    needs_distractors = 3

    def build(self, ctx: BuildContext) -> dict:
        distractors = pick_distractors(ctx, 3)
        return {
            "mode": self.key,
            "direction": self.direction,
            "prompt": self.prompt,
            "name": str(ctx.target["display_name"]),
            "photo_url": None,
            "options": face_options(ctx.target, distractors, ctx.rng),
            "hintable": False,
        }

    def grade(self, target: dict, payload: dict, answer: dict) -> Outcome:
        return grade_choice(target, answer)
