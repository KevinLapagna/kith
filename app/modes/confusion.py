"""The confusion drill: the two names you actually swap, forced apart.

Not a harder multiple choice — a narrower one. The lures are not picked for looking similar or
for belonging to the same group; they are the exact people the review log says you have already
mixed this face up with. Two options is a coin toss in general and the right question in
particular: the pair is the thing you keep getting wrong, and minimal-pair drills are how that
gets fixed.

It is only ever asked about a face with a logged mix-up (`needs_confusion`), because that log is
the only thing that makes the question meaningful — with no recorded confusion there is nothing
to drill, and the ladder moves on to another mode instead of inventing a pair.
"""

from __future__ import annotations

from app.modes.base import (
    BuildContext,
    Mode,
    Outcome,
    grade_choice,
    name_options,
    person_photo_url,
)


class Confusion(Mode):
    key = "confusion"
    label = "Confusion drill (pairs I mix up)"
    direction = "face_to_name"
    demand = 3
    min_people = 2        # target + the person they are swapped with
    input = "tap"
    needs_distractors = 1
    needs_confusion = True

    def build(self, ctx: BuildContext) -> dict:
        partner = self._partner(ctx)
        if partner is None:
            raise ValueError("no logged mix-up for this face")
        return {
            "mode": self.key,
            "direction": self.direction,
            "prompt": "Who is this?",
            "photo_url": person_photo_url(ctx.target, full=True),
            "options": name_options(ctx.target, [partner], ctx.rng),
            "hintable": False,
        }

    def grade(self, target: dict, payload: dict, answer: dict) -> Outcome:
        return grade_choice(target, answer)

    @staticmethod
    def _partner(ctx: BuildContext) -> dict | None:
        """The first logged confusable for this face that is still in the pool."""
        wanted = {int(pid) for pid in ctx.confusions.get(int(ctx.target["id"]), [])}
        for person in ctx.pool:
            if int(person["id"]) in wanted:
                return person
        return None
