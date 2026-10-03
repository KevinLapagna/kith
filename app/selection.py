"""Which due card earns the next thirty seconds.

FSRS answers *when* a card is due. It does not answer which of thirty due cards to ask first,
and ordering by `due` is the weakest available answer: it opens every session with whatever is
oldest, and age correlates with interval, and interval correlates with "you already know this
one" — the cards you find easy are exactly the ones whose intervals grow long and then sit at
the front of the queue.

So candidates are **scored**, then **sampled**, then **spread**:

  * **retrievability deficit** (1 − R) — the scheduler's own estimate that you would fail this
    card at this moment. Weighted highest, because it is the only term that comes from the
    model rather than from a hand-picked heuristic.
  * **confusion** — you have mistaken this person for another one. That is what
    `review.chosen_person_id` is for, and repairing a confusion is the point of the app, so it
    outranks generic weakness.
  * **difficulty** (FSRS 1–10, normalised) — the same deficit is more expensive to repair on a
    card the model already finds hard.
  * **overdue ratio** — how late, *relative to the card's own strength*. Three days late on a
    five-day interval means much more than three days late on a three-hundred-day one.
  * **lapses** — a card that keeps failing is an encoding problem as much as a scheduling one
    (§14), so it keeps a small nudge and never dominates.

Sampling rather than taking the maximum every time: argmax makes the order deterministic, and a
learner who notices starts learning the *order*. Weighted sampling keeps the strongest cards at
the front on average while leaving the tail reachable.

Two corrections that a score cannot express:

  * a **maintenance floor** (setting `maintenance_floor`, default 10 % of the session), drawn at
    random from the cards the model believes are *known*. Without it the trainer only ever asks
    what its own model predicts you will forget, and rots quietly on the items it has
    over-estimated.
  * **spreading near-identical faces apart** — two photos with a close perceptual hash are
    confusable as images rather than as memories, so asking them back to back measures nothing.
"""

from __future__ import annotations

import random
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from app.images import hamming
from app.repo import cards as cards_repo

# Score weights (they sum to 1.0) and the shape of the sampling distribution. These are constants
# rather than settings on purpose: an untuned knob is not configuration, and there is no evidence
# yet to tune them with. `maintenance_floor` is the exception — it was anticipated in the schema
# and is read from the settings table.
W_RETRIEVABILITY = 0.45
W_CONFUSION = 0.20
W_DIFFICULTY = 0.15
W_OVERDUE = 0.15
W_LAPSES = 0.05

SHARPNESS = 3.0           # exponent on the sampling weight: higher = closer to argmax
REWARD_FLOOR = 0.04       # so even a zero-score card keeps a non-zero chance
CONFUSION_SATURATION = 2.0  # two mix-ups saturate the term
LAPSE_SATURATION = 3.0
# The same Hamming distance `photos.similar` uses to call two faces alike — one number, two uses,
# so "looks like them" means the same thing in the distractor engine and in the queue.
DEFAULT_SIMILARITY = 14
DEFAULT_RETENTION = 0.90


@dataclass(frozen=True)
class Candidate:
    """One card, with the features that decided its place in the queue."""

    card: dict
    retrievability: float
    difficulty: float
    overdue: float
    confusion: float
    lapses: float
    score: float
    exploration: bool = False

    @property
    def card_id(self) -> int:
        return int(self.card["id"])

    @property
    def person_id(self) -> int:
        return int(self.card["person_id"])

    @property
    def reasons(self) -> dict[str, float]:
        """The scored terms, for logs and for answering "why was I asked this?"."""
        return {
            "retrievability": round(self.retrievability, 4),
            "deficit": round(1.0 - self.retrievability, 4),
            "difficulty": round(self.difficulty, 4),
            "overdue": round(self.overdue, 4),
            "confusion": round(self.confusion, 4),
            "lapses": round(self.lapses, 4),
        }


def confusion_counts(conn: sqlite3.Connection) -> dict[int, int]:
    """How often each person has been mixed up with someone else, counting both sides.

    Both sides matter: the person you were asked about and could not name, *and* the person
    whose name you reached for instead. The `review_confusion` index exists for this query.
    """
    counts: dict[int, int] = {}
    rows = conn.execute(
        """
        SELECT c.person_id AS target, r.chosen_person_id AS chosen, count(*) AS n
        FROM review r
        JOIN card c ON c.id = r.card_id
        WHERE r.correct = 0 AND r.chosen_person_id IS NOT NULL
          AND r.chosen_person_id <> c.person_id
        GROUP BY c.person_id, r.chosen_person_id
        """
    ).fetchall()
    for row in rows:
        times = int(row["n"])
        for person_id in (int(row["target"]), int(row["chosen"])):
            counts[person_id] = counts.get(person_id, 0) + times
    return counts


def score_candidates(
    conn: sqlite3.Connection,
    cards: list[dict],
    *,
    now: str,
    desired_retention: float = DEFAULT_RETENTION,
) -> list[Candidate]:
    """Feature and score every candidate card. Order is not decided here."""
    scheduler = cards_repo.make_scheduler(desired_retention)
    stamp = _parse(now)
    confusions = confusion_counts(conn)

    scored: list[Candidate] = []
    for card in cards:
        retrievability = _retrievability(scheduler, card, stamp)
        difficulty = _between(card.get("difficulty"), low=1.0, high=10.0, missing=0.5)
        overdue = _overdue_ratio(card, stamp)
        confusion = min(1.0, confusions.get(int(card["person_id"]), 0) / CONFUSION_SATURATION)
        lapses = min(1.0, int(card.get("lapses") or 0) / LAPSE_SATURATION)
        score = (
            W_RETRIEVABILITY * (1.0 - retrievability)
            + W_CONFUSION * confusion
            + W_DIFFICULTY * difficulty
            + W_OVERDUE * overdue
            + W_LAPSES * lapses
        )
        scored.append(
            Candidate(
                card=card,
                retrievability=retrievability,
                difficulty=difficulty,
                overdue=overdue,
                confusion=confusion,
                lapses=lapses,
                score=round(score, 6),
            )
        )
    return scored


def pick(
    candidates: list[Candidate],
    count: int,
    *,
    rng: random.Random,
    maintenance_floor: float = 0.10,
    desired_retention: float = DEFAULT_RETENTION,
) -> list[Candidate]:
    """Choose `count` candidates and put them in the order to ask them.

    Scored, sampled without replacement, floored with maintenance draws, and woven so those
    draws land throughout the session rather than in one lump at the end.
    """
    if count <= 0 or not candidates:
        return []

    pool = sorted(candidates, key=lambda candidate: (-candidate.score, candidate.card_id))
    maintenance_n = max(0, min(int(round(maintenance_floor * count)), count - 1))

    chosen = _weighted_sample(pool, count - maintenance_n, rng)
    booked = {candidate.card_id for candidate in chosen}
    # The floor is drawn only from cards the model believes are *known* and that scoring did not
    # already take. A "maintenance" card that the score wanted anyway would not be maintenance.
    spare = [
        candidate
        for candidate in pool
        if candidate.card_id not in booked and candidate.retrievability >= desired_retention
    ]
    maintenance: list[Candidate] = []
    while len(maintenance) < maintenance_n and spare:
        drawn = rng.choice(spare)
        spare.remove(drawn)
        maintenance.append(replace(drawn, exploration=True))

    ordered = _weave(chosen, maintenance)
    short = count - len(ordered)
    if short > 0:
        taken = {candidate.card_id for candidate in ordered}
        ordered.extend(
            _weighted_sample([c for c in pool if c.card_id not in taken], short, rng)
        )
    return ordered


def spread_apart(
    candidates: list[Candidate],
    *,
    hashes: dict[int, str | None],
    threshold: int = DEFAULT_SIMILARITY,
    window: int = 2,
) -> list[Candidate]:
    """Reorder so two lookalike faces are not asked back to back.

    Greedy and deliberately forgiving: when the queue offers nothing but lookalikes the wait is
    honoured as far as it can be, because dropping items — or inventing an order the score did
    not choose — would be worse than asking a hard pair in a row.
    """
    remaining = list(candidates)
    ordered: list[Candidate] = []
    recent: list[str] = []
    while remaining:
        allowed = [
            candidate
            for candidate in remaining
            if not any(_alike(hashes.get(candidate.person_id), digest, threshold) for digest in recent[-window:])
        ]
        take = allowed[0] if allowed else remaining[0]
        remaining.remove(take)
        ordered.append(take)
        digest = hashes.get(take.person_id)
        if digest:
            recent.append(str(digest))
    return ordered


# ── the arithmetic ───────────────────────────────────────────────────────────
def _parse(stamp: str) -> datetime:
    moment = datetime.fromisoformat(str(stamp))
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _retrievability(scheduler, card: dict, stamp: datetime) -> float:
    """How likely you are to recall this right now; 0.0 when the model cannot say.

    A card with no review behind it has no retrievability. Reporting 0.0 reads as "fully at
    risk", which is the honest default for a card the model has nothing to say about — and it
    must not crash the queue, so the library's own refusals are caught rather than trusted.
    """
    try:
        value = scheduler.get_card_retrievability(cards_repo.load(card), stamp)
    except Exception:  # noqa: BLE001 — an unreviewed card is a legitimate state, not an error
        return 0.0
    return float(value) if value is not None else 0.0


def _between(value, *, low: float, high: float, missing: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return missing
    return max(0.0, min(1.0, (number - low) / (high - low)))


def _overdue_ratio(card: dict, stamp: datetime) -> float:
    due = card.get("due")
    if not due:
        return 0.0
    late_days = (stamp - _parse(str(due))).total_seconds() / 86400.0
    if late_days <= 0:
        return 0.0
    strength = max(float(card.get("stability") or 0.0), 0.5)
    return min(1.0, late_days / strength)


def _alike(digest: str | None, other: str | None, threshold: int) -> bool:
    if not digest or not other:
        return False
    return hamming(str(digest), str(other)) <= threshold


def _weighted_sample(
    pool: list[Candidate], count: int, rng: random.Random
) -> list[Candidate]:
    """Sample without replacement, weighted by score, strongest first on average."""
    remaining = list(pool)
    drawn: list[Candidate] = []
    while remaining and len(drawn) < count:
        weights = [(candidate.score + REWARD_FLOOR) ** SHARPNESS for candidate in remaining]
        choice = rng.choices(remaining, weights=weights, k=1)[0]
        remaining.remove(choice)
        drawn.append(choice)
    return drawn


def _weave(graded: list[Candidate], maintenance: list[Candidate]) -> list[Candidate]:
    """Put the maintenance draws at even intervals through the session, not at the end."""
    if not maintenance:
        return list(graded)
    total = len(graded) + len(maintenance)
    slots = {
        min(total, int(round(index * total / (len(maintenance) + 1))))
        for index in range(1, len(maintenance) + 1)
    }
    rest = list(graded)
    spare = list(maintenance)
    woven: list[Candidate] = []
    for position in range(1, total + 1):
        if position in slots and spare:
            woven.append(spare.pop(0))
        elif rest:
            woven.append(rest.pop(0))
    woven.extend(rest)
    woven.extend(spare)
    return woven
