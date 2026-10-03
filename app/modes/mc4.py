"""Multiple choice, four names, one face — the gentlest *recognition* test.

It trains discriminating between names rather than producing one, so it is the right mode
for a face you have met but not yet learned. Four is not arbitrary: three options can be
guessed too often to be informative, and five just adds reading.
"""

from __future__ import annotations

from app.modes.base import BuildContext, Mode, Outcome, name_options, person_photo_url, pick_distractors


class MultipleChoice(Mode):
    key = "mc4"
    label = "Multiple choice — 4 names, 1 face"
    direction = "face_to_name"
    demand = 2
    min_people = 4        # target + three distractors
    input = "tap"
    needs_distractors = 3

    def build(self, ctx: BuildContext) -> dict:
        distractors = pick_distractors(ctx, 3)
        return {
            "mode": self.key,
            "direction": self.direction,
            "prompt": "Who is this?",
            "photo_url": person_photo_url(ctx.target, full=True),
            "options": name_options(ctx.target, distractors, ctx.rng),
            "hintable": False,
        }

    def grade(self, target: dict, payload: dict, answer: dict) -> Outcome:
        chosen = answer.get("person_id")
        if chosen is None:
            # As in free recall: refuse rather than log a miss nobody made.
            raise ValueError("an answer needs the person_id you chose")
        try:
            chosen_id = int(chosen)
        except (TypeError, ValueError):
            raise ValueError("person_id must be a number") from None
        correct = chosen_id == int(target["id"])
        return Outcome(
            correct=correct,
            chosen_person_id=chosen_id,
            latency_ms=_latency(answer),
        )


def _latency(answer: dict) -> int | None:
    raw = answer.get("latency_ms")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return max(0, min(value, 600_000))    # clamp: a stray client value must not skew ratings
