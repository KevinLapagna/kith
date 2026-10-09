"""Produce the name yourself: think (or say it out loud), reveal, then rate honestly.

This is the mode that matters in real life, and the only one that needs no reading of
options. It is deliberately one tap of thought: reveal, then 1–4. No typing, ever.

Grading is self-reported, so `derived` is False for these answers — you rated it, the app
did not infer it. "I have no idea" is recorded separately from a wrong answer: it means the
face was never encoded, which calls for a study card rather than another test.
"""

from __future__ import annotations

from app.modes.base import (
    BuildContext,
    Mode,
    Outcome,
    hints_for,
    person_photo_url,
)


class RecallSelf(Mode):
    key = "recall_self"
    label_key = "mode.recall_self.label"
    prompt_key = "mode.recall_self.prompt"
    direction = "face_to_name"
    demand = 5
    min_people = 1
    input = "self"
    needs_distractors = 0

    def build(self, ctx: BuildContext) -> dict:
        name = str(ctx.target["display_name"])
        return {
            "mode": self.key,
            "direction": self.direction,
            "prompt": self.prompt,
            "photo_url": person_photo_url(ctx.target, full=True),
            "options": [],
            "hintable": True,
            "hint_count": len(hints_for(name)),
        }

    def grade(self, target: dict, payload: dict, answer: dict) -> Outcome:
        if answer.get("no_idea"):
            return Outcome(correct=False, no_idea=True, self_rating=1, hint_level=0,
                           latency_ms=_latency(answer))
        rating = _coerce_rating(answer.get("self_rating"))
        if rating is None:
            # Never invent a rating. A missing one used to default to "Good", which would
            # quietly write a false success into the log the scheduler runs on.
            raise ValueError("an answer needs either a self_rating (1-4) or no_idea")
        hint_level = _coerce_hint(answer.get("hint_level"))
        if hint_level >= 3:
            rating = 1          # the whole ladder used: treat as a miss, as documented
        return Outcome(
            correct=rating >= 2,
            self_rating=rating,
            hint_level=hint_level,
            latency_ms=_latency(answer),
        )


def _coerce_rating(value: object) -> int | None:
    try:
        rating = int(value)          # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return rating if 1 <= rating <= 4 else None


def _coerce_hint(value: object) -> int:
    try:
        level = int(value)           # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    return max(0, min(level, 5))     # the ladder has five rungs


def _latency(answer: dict) -> int | None:
    try:
        value = int(answer.get("latency_ms"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return max(0, min(value, 600_000))
