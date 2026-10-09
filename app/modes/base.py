"""Exercise modes as small self-registering plugins.

The **server** builds each payload and grades every answer; the browser only renders and
collects input. One definition of "correct", and every mode is testable without a browser.

Modes declare what they need (`min_people`, `needs_distractors`), so a mode the pool is
too small for is simply never offered — the "fewer than four people → free recall instead
of multiple choice" behaviour falls out of the data rather than being special-cased.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from app import i18n


@dataclass(frozen=True)
class Outcome:
    """What the trainer needs to know after an answer."""

    correct: bool
    chosen_person_id: int | None = None   # the lure tapped → confusion matrix
    hint_level: int = 0
    no_idea: bool = False                 # "never seen them" ≠ a wrong answer
    self_rating: int | None = None        # 1..4 when the learner rated themselves
    latency_ms: int | None = None


@dataclass
class BuildContext:
    """Everything a mode may use to build one prompt."""

    target: dict                          # the person row being asked about
    pool: list[dict]                      # candidates for distractors (same shape)
    rng: random.Random
    recent_person_ids: list[int] = field(default_factory=list)
    photo_id: int | None = None           # which photo to show, if any
    confusions: dict[int, list[int]] = field(default_factory=dict)   # person → who they are swapped with
    board: list[dict] = field(default_factory=list)   # a board item's roster, target first


class Mode:
    """Base class: subclasses set the attributes and implement build/grade."""

    key: str = ""
    label_key: str = ""                   # catalogue key, resolved per request by `label`
    prompt_key: str = ""                  # catalogue key, resolved per request by `prompt`
    direction: str = "face_to_name"       # face_to_name | name_to_face | none
    demand: int = 3                       # 1..5, drives adaptive difficulty (M3/M4)
    min_people: int = 1
    input: str = "tap"                    # tap | drag | self | none
    needs_distractors: int = 0
    needs_confusion: bool = False         # only askable for a face with a logged mix-up
    gradeable: bool = True                # False for study cards (no retrieval happens)
    board: bool = False                   # one item, several people (match 3 / match 6)
    board_size: int = 0                   # how many, when board is True
    hintable: bool = False                # False for modes where a hint makes no sense

    @property
    def label(self) -> str:
        """The builder's name for this mode, from the catalogue."""
        return i18n.t(self.label_key)

    @property
    def prompt(self) -> str:
        """The line the run screen shows above the question, from the catalogue."""
        return i18n.t(self.prompt_key)

    def build(self, ctx: BuildContext) -> dict:  # pragma: no cover - interface
        raise NotImplementedError

    def grade(self, target: dict, payload: dict, answer: dict) -> Outcome:  # pragma: no cover
        raise NotImplementedError

    def grade_board(self, roster: list[dict], payload: dict, answer: dict) -> list[dict]:
        """Grade a multi-person item: one result per member. Only for `board` modes."""
        raise NotImplementedError


# ── shared helpers ───────────────────────────────────────────────────────────
def person_photo_url(person: dict, *, full: bool = False) -> str | None:
    photo_id = person.get("photo_id")
    if not photo_id:
        return None
    return f"/photos/{int(photo_id)}.webp" if full else f"/photos/{int(photo_id)}/thumb.webp"


def _hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def pick_distractors(ctx: BuildContext, count: int) -> list[dict]:
    """Choose plausible companions for the target, best-first then randomly.

    Preference order is the point: same group first (people you meet in the same place are
    the ones you mix up), then visually similar faces by perceptual hash, then everyone
    else. Candidates recently asked are pushed to the back so the same face does not come
    straight back at you.
    """
    target_id = int(ctx.target["id"])
    target_groups = set(int(g) for g in (ctx.target.get("group_ids") or []))
    target_hash = ctx.target.get("dhash")
    recent = set(int(p) for p in ctx.recent_person_ids)

    scored: list[tuple[float, dict]] = []
    for person in ctx.pool:
        if int(person["id"]) == target_id:
            continue
        score = ctx.rng.random()          # the tie-breaker, and the whole ranking when nothing else applies
        shared = target_groups & set(int(g) for g in (person.get("group_ids") or []))
        if shared:
            score += 2.0
        if target_hash and person.get("dhash"):
            try:
                distance = _hamming(str(target_hash), str(person["dhash"]))
            except (ValueError, TypeError):
                distance = 64
            if distance <= 12:
                score += 1.5              # looks alike → a genuinely hard choice
            elif distance >= 40:
                score -= 0.3              # obviously different → weak distractor
        if int(person["id"]) in recent:
            score -= 1.0
        scored.append((score, person))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [person for _, person in scored[:count]]


def name_options(target: dict, others: list[dict], rng: random.Random) -> list[dict]:
    """Target + distractors as tappable name chips, in a stable-but-arbitrary order."""
    options = [{"person_id": int(target["id"]), "label": target["display_name"]}]
    options += [
        {"person_id": int(p["id"]), "label": p["display_name"]} for p in others
    ]
    rng.shuffle(options)
    return options


def face_options(target: dict, others: list[dict], rng: random.Random) -> list[dict]:
    """Target + distractors as tappable face tiles, in a stable-but-arbitrary order.

    Deliberately **without** labels, unlike `name_options`: when the answer is the face, a
    caption under each tile would be the answer key printed on the question.
    """
    options = [{"person_id": int(target["id"]), "photo_url": person_photo_url(target)}]
    options += [
        {"person_id": int(p["id"]), "photo_url": person_photo_url(p)} for p in others
    ]
    rng.shuffle(options)
    return options


def latency_ms(answer: dict) -> int | None:
    """The client's own timing, clamped — a stray value must not skew a derived rating."""
    try:
        value = int(answer.get("latency_ms"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return max(0, min(value, 600_000))


def grade_choice(target: dict, answer: dict) -> Outcome:
    """Grade a tap on a person: right, wrong, or refused — never an invented outcome.

    The tap is compared with the person the *server* knows the item is about. Nothing the
    browser says about which option is the right one is ever read.
    """
    chosen = answer.get("person_id")
    if chosen is None:
        # As in free recall: refuse rather than log a miss nobody made.
        raise ValueError("an answer needs the person_id you chose")
    try:
        chosen_id = int(chosen)
    except (TypeError, ValueError):
        raise ValueError("person_id must be a number") from None
    return Outcome(
        correct=chosen_id == int(target["id"]),
        chosen_person_id=chosen_id,
        latency_ms=latency_ms(answer),
    )


def syllables(word: str) -> int:
    """Rough syllable count — good enough for a hint that says "three syllables".

    Counting vowel groups is wrong for English (silent e, diphthongs), which is fine: a
    hint is a cue, not a phonology lecture, and it is only ever shown on request.
    """
    groups = 0
    previous_vowel = False
    for char in word.lower():
        vowel = char in "aeiouyàáâäåèéêëìíîïòóôöùúûü"
        if vowel and not previous_vowel:
            groups += 1
        previous_vowel = vowel
    return max(1, groups)


def hints_for(name: str) -> list[str]:
    """The cueing ladder (§5.3): first letter → shape → two letters → half → the name.

    Progressive cues rather than a dead end: the learner is never left sitting in failure,
    the support just fades.
    """
    clean = " ".join(name.split())
    parts = clean.split()
    first = parts[0] if parts else clean
    letters = [c for c in first if c.isalpha()]
    opening = (letters[0].upper() + "…") if letters else "…"
    two = ("".join(letters[:2]).title() + "…") if len(letters) > 1 else opening
    half = first[: max(1, len(first) // 2 + 1)] + "…"
    words = len(parts)
    count = syllables(first)
    shape = (
        i18n.t("mode.hint.syllables_one", count=count)
        if count == 1
        else i18n.t("mode.hint.syllables_many", count=count)
    )
    if words > 1:
        shape += i18n.t("mode.hint.words", count=words)
    return [opening, shape, two, half, clean]
