"""The trainer: build a queue, ask one item at a time, grade it, log the review.

Everything authoritative lives here and in SQLite, not in the browser. The client holds a
session id and an item id; it never holds the answer, and it cannot invent one — grading
compares the submitted choice against the person the server already knows the item is about.

The two rules worth knowing before reading:

  * **Server builds, client renders.** Every payload is rebuilt server-side on demand and
    seeded by item id, so re-rendering a prompt (a resumed session, a refresh) shows the
    same thing without the server having to store the prompt.
  * **The FSRS rating is derived from your behaviour** (§8.1) — hint use, speed against
    your own median for that face, and whether the card was already stable. You only rate
    yourself in `recall_self`, and then `derived=0` records that you did.
"""

from __future__ import annotations

import random
import sqlite3
from dataclasses import dataclass

from fsrs import Rating

from app import modes as mode_registry
from app import selection
from app.db import utcnow
from app.modes.base import BuildContext, Outcome, person_photo_url
from app.repo import cards as cards_repo
from app.repo import people as people_repo
from app.repo import reviews as reviews_repo
from app.repo import sessions as sessions_repo
from app.repo import settings as settings_repo

RATING_TO_FSRS = {1: Rating.Again, 2: Rating.Hard, 3: Rating.Good, 4: Rating.Easy}
ITEMS_PER_MINUTE = 3
DEFAULT_LATENCY_MS = 6000
MAX_REQUEUES = 3            # §8.2: then the session lets it go, rather than grinding
REQUEUE_AFTER = (5, 10, 15)  # where a missed item goes back, per requeue count
REQUEUE_AFTER_CORRECT = 8
FALLBACK_MEDIAN_MS = 6000


class NoPeople(Exception):
    """Nothing trainable for the chosen filter — a friendly page, not a crash."""


class NoModes(Exception):
    """The pool is too small for every mode that was enabled."""


@dataclass(frozen=True)
class Tuning:
    retire_count: int = 1
    window: int = 4
    slow: float = 1.5
    fast: float = 0.6
    maintenance_floor: float = 0.10

    @classmethod
    def load(cls, conn: sqlite3.Connection) -> "Tuning":
        return cls(
            retire_count=settings_repo.get_int(conn, "retire_count", 1),
            window=settings_repo.get_int(conn, "interleave_window", 4),
            slow=settings_repo.get_float(conn, "slow_factor", 1.5),
            fast=settings_repo.get_float(conn, "fast_factor", 0.6),
            maintenance_floor=settings_repo.get_float(conn, "maintenance_floor", 0.10),
        )


# ── the pool ─────────────────────────────────────────────────────────────────
def training_pool(
    conn: sqlite3.Connection,
    *,
    grp_ids: list[int] | None = None,
    person_ids: list[int] | None = None,
) -> list[dict]:
    """Trainable people, with the facets a mode needs (a photo, its hash, group memberships).

    The photo is the *primary* one, falling back to the oldest: photo variety as a teaching
    tool needs the rest of them, which M4 adds.
    """
    rows = conn.execute(
        f"""
        SELECT p.id, p.nickname, p.mnemonic, p.where_met, p.feature_note, p.family_name,
               {people_repo.RECALL_NAME} AS display_name,
               p.notes,
               (SELECT ph.id FROM photo ph WHERE ph.person_id = p.id
                 ORDER BY ph.is_primary DESC, ph.id LIMIT 1) AS photo_id,
               (SELECT ph.dhash FROM photo ph WHERE ph.person_id = p.id
                 ORDER BY ph.is_primary DESC, ph.id LIMIT 1) AS dhash
        FROM person p
        WHERE p.status = 'active'
        ORDER BY {people_repo.RECALL_NAME} COLLATE NOCASE, p.id
        """
    ).fetchall()
    memberships: dict[int, list[int]] = {}
    for row in conn.execute("SELECT grp_id, person_id FROM grp_member"):
        memberships.setdefault(int(row["person_id"]), []).append(int(row["grp_id"]))

    pool: list[dict] = []
    for row in rows:
        person = dict(row)
        person["group_ids"] = memberships.get(int(person["id"]), [])
        if grp_ids and not (set(grp_ids) & set(person["group_ids"])):
            continue
        if person_ids is not None and int(person["id"]) not in set(person_ids):
            continue
        pool.append(person)
    return pool


# Everything an item needs about the person it is about. The name is the *resolved* one, never
# the raw override column, so a first name that was corrected is asked for at the next prompt.
_PERSON_SELECT = f"""
    p.id, p.given_name, p.family_name, p.nickname, p.pronunciation, p.mnemonic, p.feature_note,
    p.where_met, p.met_on, p.notes, p.status, p.paused_until, p.created_at, p.updated_at,
    {people_repo.RECALL_NAME} AS display_name,
    (SELECT ph.id FROM photo ph WHERE ph.person_id = p.id
      ORDER BY ph.is_primary DESC, ph.id LIMIT 1) AS photo_id
"""


def target_person(conn: sqlite3.Connection, item: dict) -> dict | None:
    """The person an item is about — via its card, or directly for a new person."""
    if item.get("card_id"):
        row = conn.execute(
            f"SELECT {_PERSON_SELECT} FROM card c JOIN person p ON p.id = c.person_id WHERE c.id = ?",
            (int(item["card_id"]),),
        ).fetchone()
        return dict(row) if row else None
    if item.get("new_person_id"):
        row = conn.execute(
            f"SELECT {_PERSON_SELECT} FROM person p WHERE p.id = ?",
            (int(item["new_person_id"]),),
        ).fetchone()
        return dict(row) if row else None
    return None


def recent_people(conn: sqlite3.Connection, session_id: int, item_id: int, window: int) -> list[int]:
    """People asked in the last few answered items — kept out of the distractor pool."""
    rows = conn.execute(
        """
        SELECT si.card_id, si.new_person_id FROM session_item si
        WHERE si.session_id = ? AND si.state = 'answered' AND si.position <
              (SELECT position FROM session_item WHERE id = ?)
        ORDER BY si.position DESC LIMIT ?
        """,
        (session_id, item_id, window),
    ).fetchall()
    people: list[int] = []
    for row in rows:
        person = None
        if row["card_id"]:
            card = cards_repo.get(conn, int(row["card_id"]))
            person = card["person_id"] if card else None
        elif row["new_person_id"]:
            person = int(row["new_person_id"])
        if person is not None:
            people.append(int(person))
    return people


def choose_mode(
    modes: list,
    card: dict | None,
    *,
    direction: str | None = None,
    last_by_card: dict[int, dict] | None = None,
    confusion_people: set[int] | None = None,
    person_id: int | None = None,
):
    """The mode to ask this card in — one rung at a time, inside what you ticked (§9.3).

    `app/ladder.py` holds the rule; this picks the tiers for the card at hand and hands the
    ladder its evidence. Modes that cannot be built for this face (a confusion drill with no
    logged mix-up) leave the tiers here, so the ladder moves on instead of asking for a
    question that has no answer to build.
    """
    from app import ladder
    from app.repo import cards as cards_repo

    if not modes:
        return None
    where = direction or str((card or {}).get("direction") or cards_repo.FACE_TO_NAME)
    tiers = ladder.tiers_for(list(modes), where)
    if confusion_people is not None and person_id is not None and int(person_id) not in confusion_people:
        tiers = [mode for mode in tiers if not mode.needs_confusion]
    if not tiers:
        return None
    card_id = int(card["id"]) if card and card.get("id") else None
    return ladder.choose(
        tiers,
        last=(last_by_card or {}).get(card_id) if card_id is not None else None,
        reps=int((card or {}).get("reps") or 0),
        lapses=int((card or {}).get("lapses") or 0),
    )


def interleave(chunks: list[list[dict]], window: int) -> list[list[dict]]:
    """Reorder *chunks* so the same person never comes back within `window` items.

    Chunks, not items, because a study card and the question that follows it are one
    teaching moment — shuffling them apart would ask about a face before showing it.
    Greedy, with a fallback: if nothing is currently allowed the wait is honoured as far as
    the queue permits rather than dropping the item, so a short queue still runs.
    """
    if window <= 1:
        return list(chunks)
    remaining = list(chunks)
    ordered: list[list[dict]] = []
    recent: list[int] = []
    while remaining:
        allowed = [
            chunk for chunk in remaining if int(chunk[0]["person_id"]) not in recent[-window:]
        ]
        take = allowed[0] if allowed else remaining[0]
        remaining.remove(take)
        ordered.append(take)
        recent.append(int(take[0]["person_id"]))
    return ordered


# ── building a session ───────────────────────────────────────────────────────
def build_session(
    conn: sqlite3.Connection,
    *,
    mode_keys: list[str],
    grp_ids: list[int] | None = None,
    person_ids: list[int] | None = None,
    new_budget: int = 3,
    minutes: int = 10,
    planned_items: int | None = None,
    retire_count: int | None = None,
    include_all: bool = False,
    rng: random.Random | None = None,
    now: str | None = None,
) -> int:
    """Persist a new session and its queue, and return the session id."""
    now = now or utcnow()
    rng = rng or random.Random()
    tuning = Tuning.load(conn)
    retire_count = tuning.retire_count if retire_count is None else int(retire_count)

    pool = training_pool(conn, grp_ids=grp_ids, person_ids=person_ids)
    if not pool:
        raise NoPeople("nobody to train on with that filter")

    from app import ladder

    usable = mode_registry.available(len(pool), keys=mode_keys)
    graded = [
        mode for mode in usable if mode.gradeable and mode.direction in cards_repo.DIRECTIONS
    ]
    if not graded:
        raise NoModes("the enabled modes need more people than the cabinet has")

    # ── two skills, two cards, two ladders ───────────────────────────────────
    # `face_to_name` is the primary axis, and the one a never-seen face is introduced on. A
    # `name_to_face` card (reverse, listening) is made lazily and only for someone you have
    # already met the right way round: asking a face backwards before it has ever been shown
    # is a question nobody could answer.
    tiers = {
        direction: ladder.tiers_for(usable, direction) for direction in cards_repo.DIRECTIONS
    }
    directions_in_play = [d for d in cards_repo.DIRECTIONS if tiers[d]]

    planned = int(planned_items) if planned_items else max(1, int(minutes) * ITEMS_PER_MINUTE)

    person_ids_in_pool = [int(p["id"]) for p in pool]
    placeholders = ",".join("?" for _ in person_ids_in_pool)
    primary = {
        int(card["person_id"]): dict(card)
        for card in conn.execute(
            "SELECT * FROM card WHERE direction = ? AND person_id IN"
            f" ({placeholders}) AND suspended = 0",
            [cards_repo.FACE_TO_NAME, *person_ids_in_pool],
        )
    }
    cards_by_key: dict[tuple[int, str], dict] = {
        (person_id, cards_repo.FACE_TO_NAME): card
        for person_id, card in primary.items()
        if cards_repo.FACE_TO_NAME in directions_in_play
    }
    for direction in directions_in_play:
        if direction == cards_repo.FACE_TO_NAME:
            continue
        existing = {
            int(row["person_id"]): dict(row)
            for row in conn.execute(
                "SELECT * FROM card WHERE direction = ? AND person_id IN"
                f" ({placeholders}) AND suspended = 0",
                [direction, *person_ids_in_pool],
            )
        }
        for person_id in primary:
            if person_id in existing:
                cards_by_key[(person_id, direction)] = existing[person_id]
                continue
            created = dict(cards_repo.ensure(conn, person_id, direction))
            if int(created.get("suspended") or 0):
                continue
            # A card made just now is due just now: `now` was sampled a moment before this row
            # existed, so without the nudge the fresh card falls outside the due window for the
            # very session that created it — and a first reverse run would find nothing to ask.
            if str(created["due"]) > now:
                created["due"] = now
            cards_by_key[(person_id, direction)] = created

    # ── which card, not merely when it was due (see app/selection.py) ────────
    # `include_all` is the "practice anyway" switch: with nothing due — or whenever you just
    # want another run over the same faces — it draws on every card. Either way the *order* is
    # the selection engine's: scored by what you are about to forget rather than by age,
    # sampled without replacement, floored with a little maintenance, and spread so that
    # lookalike faces do not arrive back to back.
    wanted = (
        list(cards_by_key.values())
        if include_all
        else [card for card in cards_by_key.values() if str(card["due"]) <= now]
    )
    candidates = selection.score_candidates(conn, wanted, now=now)
    # The engine decides the session's *membership*, not merely its order: hand it the same
    # budget the cap below enforces, so the maintenance floor is guaranteed a place in the
    # session rather than being incidental to the order and trimmed away afterwards.
    ordered_cards = selection.spread_apart(
        selection.pick(
            candidates,
            min(len(candidates), planned),
            rng=rng,
            maintenance_floor=tuning.maintenance_floor,
        ),
        hashes={int(person["id"]): person.get("dhash") for person in pool},
        window=max(2, tuning.window // 2),
    )
    # ── the ladder's evidence: one query for the whole queue ─────────────────
    confusions = confusion_map(conn, pool)
    confusion_people = {int(person_id) for person_id in confusions}
    last_by_card = ladder.last_answers(conn)

    people_by_id = {int(person["id"]): person for person in pool}

    specs_chunks: list[list[dict]] = []
    unusable = 0
    for candidate in ordered_cards:
        card = candidate.card
        person_id = int(card["person_id"])
        mode = choose_mode(
            usable,
            card,
            last_by_card=last_by_card,
            confusion_people=confusion_people,
            person_id=person_id,
        )
        if mode is None:
            # No mode that can be built for this face (the confusion drill, ticked alone, on a
            # face nobody has ever mixed up). Skip the card; do not abandon the session.
            unusable += 1
            continue
        spec = {"card_id": int(card["id"]), "person_id": person_id, "mode": mode.key}
        if mode.board:
            # A board is the target plus its confusable neighbours, and the roster is *written
            # into the item* rather than picked again when the answer comes in: the pick reads
            # what has been asked recently, and a study card requeued earlier in the queue would
            # shift it, so a recomputed board could differ from the one on screen. A companion
            # with no card on this axis rides along as a face to tell apart (card_id None) — it
            # is graded for the verdict and never written to a schedule.
            from app.modes.match import roster_for

            members = roster_for(pool, people_by_id[person_id], mode.board_size, rng)
            spec["people"] = [
                {
                    "person_id": int(member["id"]),
                    "card_id": (
                        int(cards_by_key[(int(member["id"]), cards_repo.FACE_TO_NAME)]["id"])
                        if (int(member["id"]), cards_repo.FACE_TO_NAME) in cards_by_key
                        else None
                    ),
                }
                for member in members
            ]
        specs_chunks.append([spec])

    # ── then new faces, each introduced by its study card (one chunk, never split) ──
    fresh = [p for p in pool if int(p["id"]) not in primary]
    rng.shuffle(fresh)
    for person in fresh[: max(0, int(new_budget))]:
        chunk = [
            {
                "new_person_id": int(person["id"]),
                "person_id": int(person["id"]),
                "mode": "study",
            }
        ]
        mode = choose_mode(
            usable,
            None,
            direction=cards_repo.FACE_TO_NAME,
            confusion_people=confusion_people,
            person_id=int(person["id"]),
        )
        if mode is not None:
            chunk.append(
                {
                    "new_person_id": int(person["id"]),
                    "person_id": int(person["id"]),
                    "mode": mode.key,
                }
            )
        specs_chunks.append(chunk)

    if not specs_chunks:
        if unusable:
            raise NoModes(
                "the modes you ticked have nothing to ask here - the confusion drill needs a "
                "mix-up you have actually made"
            )
        if not primary and any(
            tiers[direction]
            for direction in cards_repo.DIRECTIONS
            if direction != cards_repo.FACE_TO_NAME
        ):
            # Only a backwards mode was ticked and there is nobody to ask backwards: a name→face
            # card is made for a face you have already met the right way round.
            raise NoModes(
                "reverse and listening come online for faces you have already met once - tick "
                "multiple choice alongside them for a first run, or practise anyway"
            )
        raise NoPeople(
            "nothing is due and no new faces were asked for - tick 'practise anyway' "
            "and start again to go over these faces regardless"
        )

    # Cap on what you are actually asked to recall; a study card rides along free.
    capped: list[dict] = []
    graded_count = 0
    for chunk in interleave(specs_chunks, tuning.window):
        if graded_count >= planned:
            break
        for spec in chunk:
            capped.append(spec)
            if spec["mode"] != "study":
                graded_count += 1

    session_id = sessions_repo.create(
        conn,
        modes=[mode.key for mode in usable],
        target_kind="groups" if grp_ids else ("people" if person_ids else "everything"),
        grp_ids=grp_ids,
        person_ids=person_ids,
        new_budget=int(new_budget),
        retire_count=int(retire_count),
        planned_items=graded_count,
        now=now,
    )
    sessions_repo.add_items(
        conn,
        session_id,
        [
            {
                "card_id": spec.get("card_id"),
                "new_person_id": spec.get("new_person_id"),
                "mode": spec["mode"],
                "people": spec.get("people"),
            }
            for spec in capped
        ],
    )
    return session_id


def confusion_map(conn: sqlite3.Connection, pool: list[dict]) -> dict[int, list[int]]:
    """Who each face in this pool is actually swapped with, worst pair first, from the log.

    The confusion drill may only ever be asked about a pair you really have mixed up — and only
    about a partner still in the pool, so a deleted person cannot leave a question with one
    option in it.
    """
    known = {int(person["id"]) for person in pool}
    pairs: dict[int, list[int]] = {}
    for row in reviews_repo.confusion_pairs(conn, limit=500):
        person_id = int(row["person_id"])
        partner_id = int(row["confused_with_id"])
        if person_id in known and partner_id in known:
            pairs.setdefault(person_id, []).append(partner_id)
    return pairs


# ── asking one item ──────────────────────────────────────────────────────────
def payload_for(conn: sqlite3.Connection, session: dict, item: dict) -> dict | None:
    """Rebuild the prompt for one item. Seeded by item id, so it is stable across renders."""
    mode = mode_registry.get(str(item["mode"]))
    person = target_person(conn, item)
    if mode is None or person is None:
        return None

    tuning = Tuning.load(conn)
    pool = training_pool(conn)
    rng = random.Random(f"{int(session['id'])}:{int(item['id'])}")
    # A board item's people were settled when the queue was built and are read back here, never
    # re-picked: the board on screen has to be the board that gets graded.
    board: list[dict] = []
    if mode.board:
        by_id = {int(entry["id"]): entry for entry in pool}
        board = [
            by_id[int(entry["person_id"])]
            for entry in (item.get("people") or [])
            if int(entry["person_id"]) in by_id
        ]
        if not board:
            return None      # the roster is gone (someone was deleted mid-session): skip the item
    ctx = BuildContext(
        target=person,
        pool=pool,
        rng=rng,
        recent_person_ids=recent_people(conn, int(session["id"]), int(item["id"]), tuning.window),
        confusions=confusion_map(conn, pool),
        board=board,
    )
    payload = mode.build(ctx)
    payload["item_id"] = int(item["id"])
    payload["requeues"] = int(item["requeues"])
    return payload


def envelope(conn: sqlite3.Connection, session: dict, verdict: dict | None = None) -> dict:
    """The round-trip shape: session counters, the verdict, and the next prompt."""
    session_id = int(session["id"])
    next_payload = None
    while True:
        item = sessions_repo.next_item(conn, session_id)
        if item is None:
            break
        next_payload = payload_for(conn, session, item)
        if next_payload is not None:
            break
        # The person behind this item is gone (deleted mid-session): skip and move on,
        # rather than handing the browser a prompt it cannot show.
        sessions_repo.mark_answered(conn, int(item["id"]), correct=False)
    counts = sessions_repo.counts(conn, session_id)
    return {
        "session": {"id": session_id, "ended": session.get("ended_at") is not None, **counts},
        "verdict": verdict,
        "next": next_payload,
        "done": next_payload is None,
    }


def derive_rating(
    *,
    outcome: Outcome,
    latency_ms: int | None,
    median_ms: int | None,
    stability: float | None,
    tuning: Tuning,
) -> int:
    """§8.1: derive the FSRS rating from what actually happened. One tap of thought, no form."""
    if not outcome.correct or outcome.no_idea:
        return 1                                  # Again
    if outcome.hint_level >= 3:
        return 1                                  # the whole ladder used
    if outcome.self_rating is not None:
        return int(outcome.self_rating)           # you rated it; see `derived` on the review
    baseline = median_ms or FALLBACK_MEDIAN_MS
    if latency_ms is not None and latency_ms > tuning.slow * baseline:
        return 2                                  # Hard: correct, but slow for this face
    if outcome.hint_level in (1, 2):
        return 2                                  # Hard: needed a cue
    if (
        latency_ms is not None
        and latency_ms < tuning.fast * baseline
        and (stability or 0.0) >= 21.0
    ):
        return 4                                  # Easy: instant, and the card was solid
    return 3                                      # Good


def _confidence(value: object) -> int | None:
    """The optional "how sure were you", as stored for the calibration view.

    Anything unrecognised becomes nothing rather than a guess: a fabricated confidence would
    quietly poison the one statistic that is supposed to be about your own judgement.
    """
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number in (1, 2, 3) else None


def submit(
    conn: sqlite3.Connection,
    session: dict,
    item: dict,
    answer: dict,
    *,
    now: str | None = None,
) -> dict:
    """Grade one answer, write it down, and return the verdict for the client."""
    now = now or utcnow()
    mode = mode_registry.get(str(item["mode"]))
    person = target_person(conn, item)
    if mode is None or person is None:
        raise NoPeople("that item no longer exists")

    session_id = int(session["id"])
    payload = payload_for(conn, session, item) or {}

    # ── a study card is not a test: no grade, no review, no schedule change ──
    if not mode.gradeable:
        sessions_repo.mark_answered(conn, int(item["id"]), correct=True)
        return envelope(conn, sessions_repo.get(conn, session_id) or session, verdict={
            "correct": True,
            "expected_person_id": int(person["id"]),
            "chosen_person_id": None,
            "rating": None,
            "derived": False,
            "study": True,
            "correct_name": person["display_name"],
            "correct_family": person.get("family_name"),
            "mnemonic": person.get("mnemonic"),
            "photo_url": person_photo_url(person, full=True),
            "hint_level": 0,
            "no_idea": False,
        })

    if mode.board:
        return _submit_board(conn, session, item, answer, mode=mode, now=now)

    outcome = mode.grade(person, payload, answer)

    from app import ladder

    # The item's own card is the authority, and it is the one the ladder read: a reverse or
    # listening question is scheduled on the name→face card, and grading it into the face→name
    # card would average two different skills into a single schedule.
    direction = ladder.direction_of(mode)
    card = cards_repo.get(conn, int(item["card_id"])) if item.get("card_id") else None
    if card is None:
        card = cards_repo.ensure(conn, int(person["id"]), direction)
    card_id = int(card["id"])
    tuning = Tuning.load(conn)
    median_ms = reviews_repo.median_latency_ms(conn, card_id)
    fsrs_card = cards_repo.load(card)

    rating = derive_rating(
        outcome=outcome,
        latency_ms=outcome.latency_ms,
        median_ms=median_ms,
        stability=card.get("stability"),
        tuning=tuning,
    )
    derived = outcome.self_rating is None

    reviewed, _log = cards_repo.make_scheduler(
        settings_repo.get_float(conn, "desired_retention", 0.90)
    ).review_card(fsrs_card, RATING_TO_FSRS[int(rating)])
    cards_repo.save_fsrs(conn, card_id, reviewed, now=now)
    cards_repo.bump_reps_lapses(conn, card_id, lapsed=int(rating) == 1, now=now)

    reviews_repo.insert(
        conn,
        card_id=card_id,
        session_id=session_id,
        session_item_id=int(item["id"]),
        mode=mode.key,
        correct=outcome.correct,
        rating=int(rating),
        reviewed_at=now,
        derived=derived,
        no_idea=outcome.no_idea,
        hint_level=outcome.hint_level,
        latency_ms=outcome.latency_ms,
        chosen_person_id=outcome.chosen_person_id,
        confidence=_confidence(answer.get("confidence")),
    )
    sessions_repo.record_answer(conn, session_id, correct=outcome.correct)

    # ── requeue or retire (§8.2) ─────────────────────────────────────────────
    requeues = int(item["requeues"])
    if outcome.correct:
        if int(item["correct_count"]) + 1 >= int(session["retire_count"]):
            sessions_repo.mark_answered(conn, int(item["id"]), correct=True)
        else:
            sessions_repo.requeue(conn, int(item["id"]), after_n=REQUEUE_AFTER_CORRECT)
    elif outcome.no_idea and requeues < MAX_REQUEUES:
        # Never encoded → look at it again before testing it again.
        sessions_repo.mark_answered(conn, int(item["id"]), correct=False)
        _insert_study(conn, session_id, item, person)
    elif requeues < MAX_REQUEUES:
        sessions_repo.requeue(conn, int(item["id"]), after_n=REQUEUE_AFTER[min(requeues, len(REQUEUE_AFTER) - 1)])
    else:
        sessions_repo.mark_answered(conn, int(item["id"]), correct=False)

    verdict = {
        "correct": outcome.correct,
        "expected_person_id": int(person["id"]),
        "chosen_person_id": outcome.chosen_person_id,
        "rating": int(rating),
        "derived": derived,
        "study": False,
        "correct_name": person["display_name"],
        "correct_family": person.get("family_name"),
        "mnemonic": person.get("mnemonic"),
        "where_met": person.get("where_met"),
        "feature_note": person.get("feature_note"),
        "photo_url": person_photo_url(person, full=True),
        "hint_level": outcome.hint_level,
        "no_idea": outcome.no_idea,
    }
    return envelope(conn, sessions_repo.get(conn, session_id) or session, verdict=verdict)


def _submit_board(
    conn: sqlite3.Connection,
    session: dict,
    item: dict,
    answer: dict,
    *,
    mode,
    now: str,
) -> dict:
    """Grade a board: one queue item, several people, one review per member who is trainable.

    The requeue rule is the item's, not the member's: one misplaced name brings the whole board
    back later, because the board *is* the cluster — re-asking the cluster is the drill. A clean
    board retires like any other item.

    Members without a card on this axis are graded for the verdict but never written down: they
    were good faces to tell apart and nobody to schedule, and creating a card for them here would
    put a face into the rotation that the learner was never introduced to.
    """
    from app import ladder
    from app.modes.base import latency_ms as clamp_latency

    session_id = int(session["id"])
    payload = payload_for(conn, session, item) or {}
    people_by_id = {int(person["id"]): person for person in training_pool(conn)}
    roster = [
        {
            "person_id": int(entry["person_id"]),
            "card_id": entry.get("card_id"),
        }
        for entry in (item.get("people") or [])
    ]
    results = mode.grade_board(roster, payload, answer)
    tuning = Tuning.load(conn)
    latency = clamp_latency(answer)
    direction = ladder.direction_of(mode)

    detailed: list[dict] = []
    missed = False
    for result in results:
        person = people_by_id.get(int(result["person_id"]))
        if person is None:
            continue
        correct = bool(result["correct"])
        chosen_id = result.get("chosen_person_id")
        missed = missed or not correct

        rating: int | None = None
        card_id = result.get("card_id")
        if card_id is not None:
            outcome = Outcome(correct=correct, chosen_person_id=chosen_id, latency_ms=latency)
            card = cards_repo.get(conn, int(card_id)) or cards_repo.ensure(
                conn, int(person["id"]), direction
            )
            rating = int(
                derive_rating(
                    outcome=outcome,
                    latency_ms=latency,
                    median_ms=reviews_repo.median_latency_ms(conn, int(card["id"])),
                    stability=card.get("stability"),
                    tuning=tuning,
                )
            )
            reviewed, _log = cards_repo.make_scheduler(
                settings_repo.get_float(conn, "desired_retention", 0.90)
            ).review_card(cards_repo.load(card), RATING_TO_FSRS[rating])
            cards_repo.save_fsrs(conn, int(card["id"]), reviewed, now=now)
            cards_repo.bump_reps_lapses(conn, int(card["id"]), lapsed=rating == 1, now=now)
            reviews_repo.insert(
                conn,
                card_id=int(card["id"]),
                session_id=session_id,
                session_item_id=int(item["id"]),
                mode=mode.key,
                correct=correct,
                rating=rating,
                reviewed_at=now,
                derived=True,
                no_idea=False,
                hint_level=0,
                latency_ms=latency,
                chosen_person_id=chosen_id,
                confidence=_confidence(answer.get("confidence")),
            )
            sessions_repo.record_answer(conn, session_id, correct=correct)

        chosen_name = None
        if chosen_id is not None and int(chosen_id) != int(person["id"]):
            other = people_by_id.get(int(chosen_id))
            chosen_name = other["display_name"] if other else None

        detailed.append(
            {
                "person_id": int(person["id"]),
                "name": person["display_name"],
                "family_name": person.get("family_name"),
                "correct": correct,
                "chosen_person_id": chosen_id,
                "chosen_name": chosen_name,
                "rating": rating,
                "scheduled": card_id is not None,
            }
        )

    requeues = int(item["requeues"])
    if not missed:
        if int(item["correct_count"]) + 1 >= int(session["retire_count"]):
            sessions_repo.mark_answered(conn, int(item["id"]), correct=True)
        else:
            sessions_repo.requeue(conn, int(item["id"]), after_n=REQUEUE_AFTER_CORRECT)
    elif requeues < MAX_REQUEUES:
        sessions_repo.requeue(
            conn, int(item["id"]), after_n=REQUEUE_AFTER[min(requeues, len(REQUEUE_AFTER) - 1)]
        )
    else:
        sessions_repo.mark_answered(conn, int(item["id"]), correct=False)

    target = people_by_id.get(int(roster[0]["person_id"])) if roster else None
    verdict = {
        "board": True,
        "correct": not missed,
        "results": detailed,
        "expected_person_id": int(target["id"]) if target else None,
        "correct_name": target["display_name"] if target else None,
        "correct_family": target.get("family_name") if target else None,
        "photo_url": person_photo_url(target, full=True) if target else None,
        "rating": None,
        "derived": True,
        "study": False,
        "hint_level": 0,
        "no_idea": False,
    }
    return envelope(conn, sessions_repo.get(conn, session_id) or session, verdict=verdict)


def _insert_study(conn: sqlite3.Connection, session_id: int, item: dict, person: dict) -> None:
    """Drop a study card for this person a few places ahead."""
    specs = sessions_repo.add_items(
        conn,
        session_id,
        [{"card_id": item.get("card_id"), "new_person_id": item.get("new_person_id"), "mode": "study"}],
    )
    if specs:
        sessions_repo.requeue(conn, specs[0], after_n=3)


def hint_for(conn: sqlite3.Connection, session: dict, item: dict, level: int) -> str | None:
    """The next rung of the cueing ladder — text only, never the whole answer up front."""
    from app.modes.base import hints_for

    person = target_person(conn, item)
    if person is None:
        return None
    ladder = hints_for(str(person["display_name"]))
    if level < 1:
        return None
    return ladder[min(level, len(ladder)) - 1]


def reveal_for(conn: sqlite3.Connection, session: dict, item: dict) -> dict | None:
    """What the card says about this person — fetched when Reveal is pressed.

    Deliberately *not* part of the prompt payload: the name must not sit in the page while
    you are still trying to produce it. A hint is a cue you asked for; this is the answer.
    """
    person = target_person(conn, item)
    if person is None:
        return None
    return {
        "person_id": int(person["id"]),
        "name": person["display_name"],
        "family_name": person.get("family_name"),
        "mnemonic": person.get("mnemonic"),
        "where_met": person.get("where_met"),
        "feature_note": person.get("feature_note"),
        "photo_url": person_photo_url(person, full=True),
    }


def summary(conn: sqlite3.Connection, session_id: int) -> dict:
    """End-of-session: what happened, what to look at again, and when each person returns."""
    session = sessions_repo.get(conn, session_id)
    counts = sessions_repo.counts(conn, session_id)
    per_person = reviews_repo.accuracy_by_person(conn, session_id)
    missed = [row for row in per_person if int(row["correct"] or 0) < int(row["asked"] or 0)]
    look_again = sorted(
        missed,
        key=lambda row: (int(row["correct"] or 0) - int(row["asked"] or 0), -int(row["no_idea"] or 0)),
    )[:3]

    next_due: list[dict] = []
    for row in per_person:
        card = cards_repo.for_person(conn, int(row["person_id"]))
        if card is not None:
            next_due.append(
                {
                    "person_id": int(row["person_id"]),
                    "display_name": row["display_name"],
                    "family_name": row.get("family_name"),
                    "due": card["due"],
                }
            )
    next_due.sort(key=lambda entry: str(entry["due"]))

    return {
        "session": session,
        "counts": counts,
        "accuracy": round(
            (counts["correct"] / counts["answers"] * 100) if counts["answers"] else 0.0, 1
        ),
        "per_person": per_person,
        "look_again": look_again,
        "next_due": next_due,
        "mistake_ids": [int(row["person_id"]) for row in missed],
    }
