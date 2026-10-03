"""Study card: look at the face, read the name. No question, no grading.

First exposure must not be a test. The selection engine puts one of these in front of every
never-seen person before it ever asks about them, and records no review for it — nothing
was recalled, so there is nothing to log. It is also the escape hatch for a leech: when a
name will not stick, go back to looking at it rather than failing again.
"""

from __future__ import annotations

from app.modes.base import BuildContext, Mode, Outcome, person_photo_url


class StudyCard(Mode):
    key = "study"
    label = "Study card (first look, no question)"
    direction = "none"
    demand = 1
    min_people = 1
    input = "none"
    needs_distractors = 0
    gradeable = False

    def build(self, ctx: BuildContext) -> dict:
        return {
            "mode": self.key,
            "direction": self.direction,
            "prompt": "First look — no question.",
            "photo_url": person_photo_url(ctx.target, full=True),
            "options": [],
            "hintable": False,
            "card": {
                "name": ctx.target["display_name"],
                "family_name": ctx.target.get("family_name"),
                "mnemonic": ctx.target.get("mnemonic"),
                "where_met": ctx.target.get("where_met"),
                "feature_note": ctx.target.get("feature_note"),
                "notes": ctx.target.get("notes"),
            },
        }

    def grade(self, target: dict, payload: dict, answer: dict) -> Outcome:
        return Outcome(correct=True)     # never counted; the router skips graded bookkeeping
