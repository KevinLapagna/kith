"""The trainer: queue construction, the answer loop, and the honesty rules.

These tests deliberately go through the same code the routes use — `trainer.build_session`
and `trainer.submit` — so a passing suite means the real loop was exercised, not a
re-implementation of it.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest
from fsrs import Rating

from app import db, modes, trainer
from app.repo import cards as cards_repo
from app.repo import groups as groups_repo
from app.repo import people as people_repo
from app.repo import reviews as reviews_repo
from app.repo import sessions as sessions_repo

NAMES = ["Rafa Nadal", "Yana Weiss", "Beatrice Kim", "Tomas Meier", "Nadia Osei", "Luca Rossi"]


def seed(settings, names=NAMES) -> list[int]:
    with db.connect(settings.db_path) as conn:
        db.migrate(conn)
        return [int(people_repo.create(conn, display_name=name)) for name in names]


def build(settings, **kwargs) -> int:
    with db.connect(settings.db_path) as conn:
        return trainer.build_session(
            conn,
            mode_keys=kwargs.pop("mode_keys", ["study", "mc4", "recall_self"]),
            rng=kwargs.pop("rng", random.Random(11)),
            now=kwargs.pop("now", db.utcnow()),
            **kwargs,
        )


def well_learned(conn, person_id: int, reviews: int = 4) -> int:
    """A card recalled correctly several times — the state where the ladder promotes hardest.

    Built through py-fsrs rather than by writing `reps` by hand: the ladder reads the column, so
    only the library can vouch that the column and the card state agree.
    """
    card_id = int(cards_repo.ensure(conn, person_id)["id"])
    scheduler = cards_repo.make_scheduler()
    card = cards_repo.load(cards_repo.get(conn, card_id))
    when = datetime.now(timezone.utc) - timedelta(days=30)
    for _ in range(reviews):
        card, _log = scheduler.review_card(card, Rating.Good, review_datetime=when)
        # `reps` is not a projection of the FSRS state — the answer path bumps it separately, so
        # a fixture that only reviewed the card would still look brand new to the mode ladder.
        cards_repo.bump_reps_lapses(conn, card_id, lapsed=False, now=db.utcnow())
        when += timedelta(days=1)
    cards_repo.save_fsrs(conn, card_id, card, now=db.utcnow())
    return card_id


# ── which modes a run may ask ────────────────────────────────────────────────
def answered(conn, card_id: int, mode: str, *, correct: bool = True, settled: bool = True) -> None:
    """Write what a real answer writes: a review, and a card that counts as known.

    The ladder reads the *log*, never the counter, so a fixture that only bumps `reps` exercises
    a branch no answer in the app can reach.
    """
    reviews_repo.insert(
        conn,
        card_id=card_id,
        mode=mode,
        correct=correct,
        rating=3 if correct else 1,
        reviewed_at="2026-01-01T09:00:00+00:00",
        derived=True,
    )
    if correct:
        for _ in range(3 if settled else 1):
            cards_repo.bump_reps_lapses(conn, card_id, lapsed=False, now=db.utcnow())


def swapped(conn, card_id: int, other_person_id: int) -> None:
    """Log the mistake the confusion drill exists for: the face, and the name actually tapped."""
    reviews_repo.insert(
        conn,
        card_id=card_id,
        mode="mc4",
        correct=False,
        rating=1,
        reviewed_at="2026-01-02T09:00:00+00:00",
        derived=True,
        chosen_person_id=other_person_id,
    )


def first_payload(conn, session_id: int):
    """The first item of a session, prompt already built — what the browser actually receives."""
    sheet = sessions_repo.get(conn, session_id)
    row = conn.execute(
        "SELECT * FROM session_item WHERE session_id = ? ORDER BY seq LIMIT 1", (session_id,)
    ).fetchone()
    item = dict(row)
    return sheet, item, trainer.payload_for(conn, sheet, item)


def asked_modes(conn, session_id: int) -> set[str]:
    return {
        str(row["mode"])
        for row in conn.execute(
            "SELECT mode FROM session_item WHERE session_id = ?", (session_id,)
        )
    }


def test_the_ladder_promotes_only_within_what_you_ticked(settings):
    """Ticking multiple choice *alone* must never produce a free-recall question.

    The ladder climbs the modes you ticked, one rung at a time — but "allowed" must mean
    allowed: with nothing above multiple choice ticked, there is nowhere to climb to.
    """
    people = seed(settings)
    with db.connect(settings.db_path) as conn:
        for person_id in people:
            card = cards_repo.ensure(conn, person_id)
            answered(conn, int(card["id"]), "mc4")
        asked: dict[tuple[str, ...], set[str]] = {}
        for ticked in (["mc4"], ["mc4", "recall_self"]):
            session_id = trainer.build_session(
                conn,
                mode_keys=ticked,
                new_budget=0,
                minutes=10,
                include_all=True,
                rng=random.Random(3),
                now=db.utcnow(),
            )
            asked[tuple(ticked)] = asked_modes(conn, session_id)

    assert asked[("mc4",)] == {"mc4"}, f"ticked multiple choice only, was asked {asked[('mc4',)]}"
    assert asked[("mc4", "recall_self")] == {"recall_self"}, (
        "a settled face should climb one rung when free recall is allowed"
    )


def test_a_miss_drops_a_face_back_down_the_ladder(settings):
    """Demotion must be as visible as promotion, or a bad day looks like a good one."""
    people = seed(settings)
    with db.connect(settings.db_path) as conn:
        card = cards_repo.ensure(conn, people[0])
        answered(conn, int(card["id"]), "recall_self", correct=False)
        session_id = trainer.build_session(
            conn,
            mode_keys=["mc4", "recall_self"],
            new_budget=0,
            minutes=10,
            include_all=True,
            rng=random.Random(3),
            now=db.utcnow(),
        )
        modes = asked_modes(conn, session_id)

    assert modes == {"mc4"}, f"after a miss in free recall, expected multiple choice, got {modes}"


def test_the_ladder_steps_into_the_drill_rather_than_over_it(settings):
    """A settled face climbs to the confusion drill — the next rung — not straight past it."""
    people = seed(settings)
    with db.connect(settings.db_path) as conn:
        for index, person_id in enumerate(people):
            card = cards_repo.ensure(conn, person_id)
            swapped(conn, int(card["id"]), people[(index + 1) % len(people)])
            answered(conn, int(card["id"]), "mc4")
        session_id = trainer.build_session(
            conn,
            mode_keys=["mc4", "confusion", "recall_self"],
            new_budget=0,
            minutes=10,
            include_all=True,
            rng=random.Random(3),
            now=db.utcnow(),
        )
        modes = asked_modes(conn, session_id)

    assert modes == {"confusion"}, f"settled in multiple choice, expected one rung up, got {modes}"


def test_a_drill_with_no_mix_up_never_blocks_the_ladder(settings):
    """No logged mix-up means no drill — and the ladder simply uses the next rung up."""
    people = seed(settings)
    with db.connect(settings.db_path) as conn:
        for person_id in people:
            card = cards_repo.ensure(conn, person_id)
            answered(conn, int(card["id"]), "mc4")
        session_id = trainer.build_session(
            conn,
            mode_keys=["mc4", "confusion", "recall_self"],
            new_budget=0,
            minutes=10,
            include_all=True,
            rng=random.Random(3),
            now=db.utcnow(),
        )
        modes = asked_modes(conn, session_id)

    assert modes == {"recall_self"}, f"with no pair to drill, expected free recall, got {modes}"


def test_reverse_offers_faces_and_never_prints_the_answer(settings):
    """In a name→face question the tiles *are* the answer, so they must carry no caption."""
    people = seed(settings)
    with db.connect(settings.db_path) as conn:
        for person_id in people:
            cards_repo.ensure(conn, person_id)
        session_id = trainer.build_session(
            conn,
            mode_keys=["reverse"],
            new_budget=0,
            minutes=10,
            include_all=True,
            rng=random.Random(5),
            now=db.utcnow(),
        )
        _, item, payload = first_payload(conn, session_id)
        card = cards_repo.get(conn, int(item["card_id"]))

    assert payload["mode"] == "reverse"
    assert payload["direction"] == "name_to_face"
    assert card["direction"] == "name_to_face", "a reverse question belongs on the name→face card"
    assert payload["name"], "the name is the question"
    assert len(payload["options"]) == 4
    assert all("photo_url" in option for option in payload["options"]), "every option is a face tile"
    assert all("label" not in option for option in payload["options"]), (
        "a caption under a face tile is the answer key printed on the question"
    )


def test_listening_speaks_the_name_but_does_not_print_it(settings):
    """The spoken name is the question; printing it as well would make listening decorative."""
    people = seed(settings)
    with db.connect(settings.db_path) as conn:
        for person_id in people:
            cards_repo.ensure(conn, person_id)
        session_id = trainer.build_session(
            conn,
            mode_keys=["listen"],
            new_budget=0,
            minutes=10,
            include_all=True,
            rng=random.Random(7),
            now=db.utcnow(),
        )
        _, _, payload = first_payload(conn, session_id)

    assert payload["mode"] == "listen"
    assert payload["speak"] == payload["name"]
    assert payload["name"] not in payload["prompt"], "the question must not give the answer away"
    assert all("label" not in option for option in payload["options"])


def test_the_confusion_drill_asks_the_pair_you_actually_swap(settings):
    """The M4 acceptance test: the drill finds the real pair, and only the real pair."""
    people = seed(settings)
    with db.connect(settings.db_path) as conn:
        first_card = cards_repo.ensure(conn, people[0])
        for person_id in people[1:]:
            cards_repo.ensure(conn, person_id)
        swapped(conn, int(first_card["id"]), people[1])
        session_id = trainer.build_session(
            conn,
            mode_keys=["confusion"],
            new_budget=0,
            minutes=10,
            include_all=True,
            rng=random.Random(3),
            now=db.utcnow(),
        )
        _, _, payload = first_payload(conn, session_id)
        modes = asked_modes(conn, session_id)
        rows = int(
            conn.execute(
                "SELECT count(*) FROM session_item WHERE session_id = ?", (session_id,)
            ).fetchone()[0]
        )
        target = str(people_repo.get(conn, people[0])["display_name"])
        lure = str(people_repo.get(conn, people[1])["display_name"])

    assert modes == {"confusion"}
    assert rows == 1, "only the face that was actually swapped should be drilled"
    labels = {str(option["label"]) for option in payload["options"]}
    assert labels == {target, lure}, f"expected the real pair, got {labels}"


def test_the_drill_refuses_rather_than_inventing_a_pair(settings):
    """Ticking the drill alone, with nothing ever mixed up, is refused in plain words."""
    people = seed(settings)
    with db.connect(settings.db_path) as conn:
        for person_id in people:
            cards_repo.ensure(conn, person_id)
        with pytest.raises(trainer.NoModes):
            trainer.build_session(
                conn,
                mode_keys=["confusion"],
                new_budget=0,
                minutes=10,
                include_all=True,
                rng=random.Random(3),
                now=db.utcnow(),
            )


def test_a_face_never_met_is_not_asked_backwards(settings):
    """Reverse and listening come online for faces you have already met the right way round."""
    people = seed(settings)
    with db.connect(settings.db_path) as conn:
        for person_id in people[:2]:
            cards_repo.ensure(conn, person_id)
        session_id = trainer.build_session(
            conn,
            mode_keys=["mc4", "reverse"],
            new_budget=4,
            minutes=10,
            rng=random.Random(3),
            now=db.utcnow(),
        )
        fresh_modes = {
            str(row["mode"])
            for row in conn.execute(
                "SELECT mode FROM session_item WHERE session_id = ? AND new_person_id IS NOT NULL",
                (session_id,),
            )
        }
        backward = cards_repo.for_person(conn, people[3], "name_to_face")

    assert fresh_modes <= {"study", "mc4"}, f"a brand-new face was asked {fresh_modes}"
    assert backward is None, "no name→face card should exist for a face never introduced"


def test_a_single_ticked_mode_keeps_a_never_seen_face_on_it_as_well(settings):
    """The other end of the ladder: a brand-new card is asked the gentlest *ticked* mode."""
    people = seed(settings)
    with db.connect(settings.db_path) as conn:
        cards_repo.ensure(conn, people[0])
        session_id = trainer.build_session(
            conn,
            mode_keys=["mc4"],
            new_budget=0,
            minutes=10,
            include_all=True,
            rng=random.Random(3),
            now=db.utcnow(),
        )
        modes_used = {
            str(row["mode"])
            for row in conn.execute(
                "SELECT mode FROM session_item WHERE session_id = ?", (session_id,)
            )
        }

    assert modes_used == {"mc4"}


def ticked(html: str, key: str) -> bool:
    """Whether the builder's checkbox for `key` came back ticked."""
    parts = html.split(f'value="{key}"', 1)
    assert len(parts) == 2, f"{key} is missing from the builder"
    return "checked" in parts[1].split(">", 1)[0]


def just_learned(conn, person_id: int) -> int:
    """A card answered an hour ago — real FSRS state, due in the future, so nothing is due."""
    card_id = int(cards_repo.ensure(conn, person_id)["id"])
    scheduler = cards_repo.make_scheduler()
    card = cards_repo.load(cards_repo.get(conn, card_id))
    card, _log = scheduler.review_card(
        card, Rating.Easy, review_datetime=datetime.now(timezone.utc) - timedelta(hours=1)
    )
    cards_repo.bump_reps_lapses(conn, card_id, lapsed=False, now=db.utcnow())
    cards_repo.save_fsrs(conn, card_id, card, now=db.utcnow())
    return card_id


def test_the_builder_does_not_recommend_study_cards(client, settings):
    """`study` is not something to tick: a never-seen face gets a study card whatever you pick."""
    seed(settings)
    html = client.get("/train").text
    assert ticked(html, "mc4"), "the recommended multiple-choice default is missing"
    assert ticked(html, "recall_self"), "the recommended recall default is missing"
    assert not ticked(html, "study"), "study must not be a recommended default"


def test_the_builder_remembers_what_you_chose(client, settings):
    """A reload must not quietly re-default the form — the WHO section included."""
    people = seed(settings)
    with db.connect(settings.db_path) as conn:
        group_id = groups_repo.create(conn, "Padel crew")
        groups_repo.set_members(conn, group_id, people[:2])

    client.post(
        "/train",
        data={"modes": ["mc4"], "grp": str(group_id), "new_budget": "5", "minutes": "20"},
        follow_redirects=False,
    )
    html = client.get("/train").text

    assert ticked(html, "mc4"), "the mode you picked was forgotten"
    assert not ticked(html, "recall_self"), "a mode you did not pick came back ticked"
    assert f'<option value="{group_id}" selected>' in html, "the group was forgotten"
    assert '<option value="5" selected>' in html, "the new-face budget was forgotten"
    assert '<option value="20" selected>' in html, "the length was forgotten"


def test_a_refused_start_remembers_the_modes_you_picked(client, settings):
    """The reported bug: "nothing is due" bounced back to a form that had re-defaulted itself."""
    people = seed(settings)
    with db.connect(settings.db_path) as conn:
        for person_id in people:
            just_learned(conn, person_id)

    response = client.post(
        "/train",
        data={"modes": ["mc4"], "new_budget": "0", "minutes": "10"},
        follow_redirects=True,
    )

    assert "nothing is due" in response.text.lower(), "the fixture should have been refused"
    assert ticked(response.text, "mc4"), "the refusal lost the mode you picked"
    assert not ticked(response.text, "recall_self"), "a mode you did not pick was ticked"
    assert 'name="force" value="1" checked' in response.text, "the refusal lost the switch"


def test_a_builder_cookie_that_makes_no_sense_is_ignored(client, settings):
    """The cookie is user input wearing a session: parse it defensively or not at all."""
    seed(settings)
    nonsense = (
        "{not json",
        '["a", "list"]',
        '{"modes": ["not_a_mode"], "minutes": 99, "grp": "x"}',
        '{"modes": "mc4", "new_budget": true}',
    )
    for raw in nonsense:
        html = client.get("/train", cookies={"kith_builder": raw}).text
        assert ticked(html, "mc4"), f"a nonsensical cookie ({raw!r}) must fall back to defaults"


# ── refusing nonsense ────────────────────────────────────────────────────────
def test_an_answer_with_no_rating_is_refused_not_guessed(settings):
    """A malformed answer must not become a silent success in the log."""
    import pytest

    seed(settings)
    session_id = build(settings, new_budget=2, minutes=10, mode_keys=["study", "recall_self"])
    with db.connect(settings.db_path) as conn:
        sheet = sessions_repo.get(conn, session_id)
        item = sessions_repo.next_item(conn, session_id)
        while item is not None and item["mode"] == "study":
            trainer.submit(conn, sheet, item, {"ack": True})
            sheet = sessions_repo.get(conn, session_id)
            item = sessions_repo.next_item(conn, session_id)
        assert item is not None and item["mode"] == "recall_self"
        before = len(reviews_repo.for_session(conn, session_id))
        with pytest.raises(ValueError):
            trainer.submit(conn, sheet, item, {"latency_ms": 1000})
        after = len(reviews_repo.for_session(conn, session_id))

    assert after == before, "a refused answer must not reach the review log"


def test_the_api_turns_a_nonsense_answer_into_a_409(client, settings):
    seed(settings)
    started = client.post(
        "/train",
        data={"modes": ["recall_self"], "minutes": "5", "new_budget": "1", "grp": ""},
        follow_redirects=False,
    )
    session_id = int(started.headers["location"].rstrip("/").rsplit("/", 1)[-1])
    state = client.get(f"/api/sessions/{session_id}").json()
    item = state["next"]
    if item["mode"] == "study":
        state = client.post(
            f"/api/sessions/{session_id}/answer", json={"item_id": item["item_id"], "ack": True}
        ).json()
        item = state["next"]
    assert item["mode"] == "recall_self"
    response = client.post(
        f"/api/sessions/{session_id}/answer", json={"item_id": item["item_id"]}
    )
    assert response.status_code == 409, response.text
    assert response.json()["ok"] is False


# ── the queue ────────────────────────────────────────────────────────────────
def play_all(settings, session_id: int, *, correct: bool = True) -> int:
    """Walk a session to its end the way the browser would. Returns how many rounds it took."""
    rounds = 0
    with db.connect(settings.db_path) as conn:
        while rounds < 200:
            rounds += 1
            sheet = sessions_repo.get(conn, session_id)
            item = sessions_repo.next_item(conn, session_id)
            if item is None:
                break
            if item["mode"] == "study":
                trainer.submit(conn, sheet, item, {"ack": True})
                continue
            target = trainer.target_person(conn, item)
            if item["mode"] == "mc4":
                if correct:
                    answer = {"person_id": int(target["id"]), "latency_ms": 1500}
                else:
                    payload = trainer.payload_for(conn, sheet, item)
                    wrong = [
                        option for option in payload["options"]
                        if int(option["person_id"]) != int(target["id"])
                    ]
                    answer = {"person_id": int(wrong[0]["person_id"]), "latency_ms": 4000}
            else:
                answer = {"self_rating": 3 if correct else 1, "latency_ms": 1500}
            trainer.submit(conn, sheet, item, answer)
    return rounds


def test_a_new_face_gets_a_study_card_before_its_question(settings):
    seed(settings)
    session_id = build(settings, new_budget=3, minutes=10)
    with db.connect(settings.db_path) as conn:
        queue = [dict(item) for item in sessions_repo.items(conn, session_id)]

    assert queue, "a session with new faces must not be empty"
    for index, item in enumerate(queue):
        if item["mode"] == "study":
            following = queue[index + 1] if index + 1 < len(queue) else None
            assert following is not None, "a study card must be followed by its question"
            assert following["mode"] != "study"
            assert following["new_person_id"] == item["new_person_id"]


def test_the_same_face_is_not_asked_again_within_the_window(settings):
    """Reviews are interleaved: a face must not come back until a few others have.

    The study card and the question that follows it are one teaching unit and are meant to
    be back to back — this is about the *second* time you meet a face in a session.
    """
    seed(settings)
    first = build(settings, new_budget=6, minutes=20)
    play_all(settings, first)

    later = "2026-12-01T09:00:00+00:00"
    second = build(settings, new_budget=0, minutes=20, now=later)
    with db.connect(settings.db_path) as conn:
        order: list[int] = []
        for item in sessions_repo.items(conn, second):
            card = cards_repo.get(conn, int(item["card_id"]))
            order.append(int(card["person_id"]))

    assert len(order) == 6
    assert len(set(order)) == 6, "a due card should appear once per session"
    for index, person_id in enumerate(order):
        assert person_id not in order[max(0, index - 4) : index], order


def test_a_small_cabinet_is_not_offered_multiple_choice(settings):
    with db.connect(settings.db_path) as conn:
        db.migrate(conn)
        for name in NAMES[:3]:
            people_repo.create(conn, display_name=name)
    assert "mc4" not in [mode.key for mode in modes.available(3)]
    assert "recall_self" in [mode.key for mode in modes.available(3)]
    assert "mc4" in [mode.key for mode in modes.available(4)]


def test_a_session_with_no_work_says_so_instead_of_starting(settings):
    seed(settings, ["Only Person"])
    with pytest.raises(trainer.NoPeople):
        build(settings, new_budget=0, minutes=5)


def test_practise_anyway_goes_over_faces_that_are_not_due(settings):
    """"Nothing is due" is not a dead end: the switch runs the same faces again on demand."""
    seed(settings, ["Only Person"])
    with db.connect(settings.db_path) as conn:
        person_id = int(conn.execute("SELECT id FROM person").fetchone()["id"])
        card = cards_repo.ensure(conn, person_id)
        # scheduled so far out that no ordinary session would touch it
        conn.execute("UPDATE card SET due = '2999-01-01T00:00:00Z' WHERE id = ?", (int(card["id"]),))
        conn.commit()

    with pytest.raises(trainer.NoPeople):
        build(settings, new_budget=0, minutes=5)

    forced = build(settings, new_budget=0, minutes=5, include_all=True)
    with db.connect(settings.db_path) as conn:
        counts = sessions_repo.counts(conn, forced)
    assert counts["planned"] >= 1, "a forced run must queue the card it was told to ignore"


# ── the name you are asked for ───────────────────────────────────────────────
def test_the_builder_hands_back_the_switch_when_nothing_is_due(client, settings):
    """The complaint this answers: "I want to learn the same people again" must not be a dead end."""
    with db.connect(settings.db_path) as conn:
        db.migrate(conn)
        person_id = int(
            people_repo.create(conn, display_name="", given_name="Yana", family_name="Weiss")
        )
        card = cards_repo.ensure(conn, person_id)
        conn.execute("UPDATE card SET due = '2999-01-01T00:00:00Z' WHERE id = ?", (int(card["id"]),))
        conn.commit()

    form = {"modes": ["study", "recall_self"], "new_budget": "0", "minutes": "5"}
    refused = client.post("/train", data=form, follow_redirects=False)
    assert refused.status_code == 303
    assert "force=1" in refused.headers["location"], "the switch comes back ticked for a second tap"
    assert "practise" in refused.headers["location"].lower()

    forced = client.post("/train", data={**form, "force": "1"}, follow_redirects=False)
    assert forced.status_code == 303, forced.text
    assert forced.headers["location"].startswith("/train/")

    run = client.get(forced.headers["location"])
    assert run.status_code == 200 and "trainer" in run.text


def test_the_builder_names_each_group_with_its_size(client, settings):
    """The dropdown read "Padel crew ()": the template asked for a field the query never returns."""
    with db.connect(settings.db_path) as conn:
        db.migrate(conn)
        yana = int(people_repo.create(conn, display_name="", given_name="Yana"))
        rafa = int(people_repo.create(conn, display_name="", given_name="Rafa"))
        group_id = int(groups_repo.create(conn, "Padel crew"))
        groups_repo.set_members(conn, group_id, [yana, rafa])

    page = client.get("/train")
    assert page.status_code == 200
    assert "Padel crew (2)" in page.text
    assert "Padel crew ()" not in page.text


def test_the_first_name_is_asked_without_typing_it_twice(settings):
    """An empty "ask for" means the first name is the answer — and an override still wins."""
    with db.connect(settings.db_path) as conn:
        db.migrate(conn)
        people_repo.create(conn, display_name="", given_name="Yana", family_name="Weiss")

    session_id = build(settings, new_budget=1, minutes=5, mode_keys=["study", "recall_self"])
    with db.connect(settings.db_path) as conn:
        sheet = sessions_repo.get(conn, session_id)
        item = sessions_repo.next_item(conn, session_id)
        card = trainer.payload_for(conn, sheet, item)["card"]
    assert card["name"] == "Yana", "the study card must introduce the name that will be asked"
    assert card["family_name"] == "Weiss", "the family name rides along to be learned on the side"

    with db.connect(settings.db_path) as conn:
        override = int(
            people_repo.create(conn, display_name="Rafa the runner", given_name="Rafael")
        )
        assert people_repo.get(conn, override)["display_name"] == "Rafa the runner"


def test_a_missed_answer_shows_the_family_name_beside_the_name(settings):
    with db.connect(settings.db_path) as conn:
        db.migrate(conn)
        people_repo.create(conn, display_name="", given_name="Yana", family_name="Weiss")

    session_id = build(settings, new_budget=1, minutes=5, mode_keys=["study", "recall_self"])
    with db.connect(settings.db_path) as conn:
        sheet = sessions_repo.get(conn, session_id)
        item = sessions_repo.next_item(conn, session_id)
        trainer.submit(conn, sheet, item, {"ack": True})  # the study card, waved through

        sheet = sessions_repo.get(conn, session_id)
        item = sessions_repo.next_item(conn, session_id)
        verdict = trainer.submit(conn, sheet, item, {"self_rating": 1, "latency_ms": 4000})["verdict"]

    assert verdict["correct"] is False
    assert verdict["correct_name"] == "Yana"
    assert verdict["correct_family"] == "Weiss"


# ── the answer loop ──────────────────────────────────────────────────────────
def test_a_correct_answer_retires_the_item(settings):
    seed(settings)
    session_id = build(settings, new_budget=1, minutes=5, mode_keys=["study", "mc4"])
    with db.connect(settings.db_path) as conn:
        sheet = sessions_repo.get(conn, session_id)
        study = sessions_repo.next_item(conn, session_id)
        assert study["mode"] == "study"
        trainer.submit(conn, sheet, study, {"ack": True})

        sheet = sessions_repo.get(conn, session_id)
        question = sessions_repo.next_item(conn, session_id)
        target = trainer.target_person(conn, question)
        response = trainer.submit(
            conn, sheet, question, {"person_id": int(target["id"]), "latency_ms": 2000}
        )

    assert response["verdict"]["correct"] is True
    assert response["verdict"]["rating"] >= 2
    assert response["session"]["retired"] == 1
    with db.connect(settings.db_path) as conn:
        retired = sessions_repo.item(conn, int(question["id"]))
    assert retired["state"] == "answered"


def test_a_missed_item_comes_back_later_in_the_same_session(settings):
    seed(settings)
    session_id = build(settings, new_budget=3, minutes=10)
    with db.connect(settings.db_path) as conn:
        sheet = sessions_repo.get(conn, session_id)
        # walk to the first graded question
        while True:
            item = sessions_repo.next_item(conn, session_id)
            if item["mode"] != "study":
                break
            trainer.submit(conn, sheet, item, {"ack": True})
            sheet = sessions_repo.get(conn, session_id)

        payload = trainer.payload_for(conn, sheet, item)
        wrong = [option for option in payload["options"] if int(option["person_id"]) != int(
            trainer.target_person(conn, item)["id"]
        )]
        answer = {"person_id": int(wrong[0]["person_id"]), "latency_ms": 5000}
        response = trainer.submit(conn, sheet, item, answer)

        assert response["verdict"]["correct"] is False
        assert response["verdict"]["chosen_person_id"] == int(wrong[0]["person_id"])
        assert response["session"]["retired"] == 0, "a miss must not count as retired"

        requeued = [entry for entry in sessions_repo.open_items(conn, session_id) if entry["id"] == item["id"]]
        assert requeued, "the missed item must be back in the queue"
        assert requeued[0]["state"] == "requeued"
        assert requeued[0]["requeues"] == 1

        # and the next prompt is not the same face straight away
        following = sessions_repo.next_item(conn, session_id)
        assert following["id"] != item["id"]


def test_saying_i_have_no_idea_shows_the_card_instead_of_testing_again(settings):
    seed(settings)
    session_id = build(settings, new_budget=2, minutes=10, mode_keys=["study", "recall_self"])
    with db.connect(settings.db_path) as conn:
        sheet = sessions_repo.get(conn, session_id)
        while True:
            item = sessions_repo.next_item(conn, session_id)
            if item["mode"] != "study":
                break
            trainer.submit(conn, sheet, item, {"ack": True})
            sheet = sessions_repo.get(conn, session_id)
        assert item["mode"] == "recall_self"

        response = trainer.submit(conn, sheet, item, {"no_idea": True, "latency_ms": 3000})
        assert response["verdict"]["no_idea"] is True
        assert response["verdict"]["rating"] == 1

        # a study card for the same person is now in the queue, and a review was logged
        studies = [
            entry for entry in sessions_repo.open_items(conn, session_id) if entry["mode"] == "study"
        ]
        assert studies, "a never-encoded face should be shown again, not only retested"
        logged = reviews_repo.for_session(conn, session_id)
        assert any(row["no_idea"] == 1 for row in logged)


def test_the_prompt_never_carries_the_answer(settings):
    """The rule from §12: nothing in the payload may give the name away before you answer."""
    seed(settings, ["Beatrice Kim", "Yana Weiss"])
    session_id = build(settings, new_budget=1, minutes=5, mode_keys=["study", "recall_self"])
    with db.connect(settings.db_path) as conn:
        sheet = sessions_repo.get(conn, session_id)
        item = sessions_repo.next_item(conn, session_id)
        # the study card is allowed to name the person — that is what it is for
        study = trainer.payload_for(conn, sheet, item)
        assert study["mode"] == "study"
        promised = study["card"]["name"]

        trainer.submit(conn, sheet, item, {"ack": True})
        item = sessions_repo.next_item(conn, session_id)
        payload = trainer.payload_for(conn, sheet, item)
        assert payload["mode"] == "recall_self"
        flattened = repr(payload)
        for token in promised.split():
            assert token not in flattened, f"the answer leaked into the prompt: {payload}"

        reveal = trainer.reveal_for(conn, sheet, item)
        assert reveal["name"] == promised, "reveal must still be able to produce the name"


# ── rating derivation (§8.1) ─────────────────────────────────────────────────
def rating_for(**kwargs) -> int:
    defaults = dict(
        latency_ms=2000, median_ms=6000, stability=5.0, tuning=trainer.Tuning(),
    )
    defaults.update(kwargs)
    return trainer.derive_rating(**defaults)


def test_rating_derivation_follows_behaviour():
    from app.modes.base import Outcome

    assert rating_for(outcome=Outcome(correct=False)) == 1
    assert rating_for(outcome=Outcome(correct=False, no_idea=True)) == 1
    assert rating_for(outcome=Outcome(correct=True, hint_level=3)) == 1
    assert rating_for(outcome=Outcome(correct=True, hint_level=1)) == 2      # needed a cue
    assert rating_for(outcome=Outcome(correct=True)) == 3                    # plain and correct
    assert rating_for(outcome=Outcome(correct=True), latency_ms=12_000) == 2  # slow for this face
    assert rating_for(outcome=Outcome(correct=True), latency_ms=1000, stability=40.0) == 4
    assert rating_for(outcome=Outcome(correct=True), latency_ms=1000, stability=3.0) == 3
    # a self-rated answer is taken at its word
    assert rating_for(outcome=Outcome(correct=True, self_rating=4)) == 4


def test_a_hint_caps_the_rating_even_when_you_were_quick():
    from app.modes.base import Outcome

    assert rating_for(outcome=Outcome(correct=True, hint_level=2), latency_ms=20_000) == 2
    assert rating_for(outcome=Outcome(correct=True, hint_level=1), latency_ms=400) == 2


# ── the HTTP surface ─────────────────────────────────────────────────────────
def test_the_builder_explains_a_cabinet_too_small_for_multiple_choice(client, settings):
    seed(settings, NAMES[:3])
    page = client.get("/train")
    assert page.status_code == 200
    assert "needs at least 4 people" in page.text
    assert "recall_self" in page.text


def test_starting_a_session_redirects_to_the_screen(client, settings):
    seed(settings)
    started = client.post(
        "/train",
        data={"modes": ["study", "mc4"], "minutes": "5", "new_budget": "2", "grp": ""},
        follow_redirects=False,
    )
    assert started.status_code == 303
    assert started.headers["location"].startswith("/train/")
    screen = client.get(started.headers["location"])
    assert screen.status_code == 200
    assert 'id="kith-bootstrap"' in screen.text
    assert "trainer.js" in screen.text


def test_answering_through_the_api_returns_a_verdict_and_the_next_prompt(client, settings):
    seed(settings)
    started = client.post(
        "/train",
        data={"modes": ["study", "mc4"], "minutes": "5", "new_budget": "1", "grp": ""},
        follow_redirects=False,
    )
    session_id = int(started.headers["location"].rsplit("/", 1)[-1])
    state = client.get(f"/api/sessions/{session_id}").json()
    assert state["ok"] is True

    # the study card first
    assert state["next"]["mode"] == "study"
    answered = client.post(
        f"/api/sessions/{session_id}/answer",
        json={"item_id": state["next"]["item_id"], "ack": True},
    ).json()
    assert answered["ok"] is True

    # then its question, answered correctly
    question = answered["next"]
    assert question["mode"] == "mc4"
    with db.connect(settings.db_path) as conn:
        item = sessions_repo.item(conn, int(question["item_id"]))
        target = trainer.target_person(conn, item)
    verdict = client.post(
        f"/api/sessions/{session_id}/answer",
        json={"item_id": question["item_id"], "person_id": int(target["id"]), "latency_ms": 1500},
    ).json()
    assert verdict["verdict"]["correct"] is True
    assert verdict["verdict"]["correct_name"] == target["display_name"]
    assert verdict["session"]["retired"] == 1


def test_an_item_from_another_session_is_refused(client, settings):
    seed(settings)
    first = client.post(
        "/train", data={"modes": ["mc4"], "minutes": "5", "new_budget": "3", "grp": ""},
        follow_redirects=False,
    ).headers["location"]
    second = client.post(
        "/train", data={"modes": ["mc4"], "minutes": "5", "new_budget": "3", "grp": ""},
        follow_redirects=False,
    ).headers["location"]
    session_two = int(second.rsplit("/", 1)[-1])
    state = client.get(f"/api/sessions/{session_two}").json()

    response = client.post(
        f"/api/sessions/{int(first.rsplit('/', 1)[-1])}/answer",
        json={"item_id": state["next"]["item_id"], "person_id": 1},
    )
    assert response.status_code == 409


def test_the_summary_page_renders_after_the_last_item(client, settings):
    seed(settings)
    started = client.post(
        "/train", data={"modes": ["study", "mc4"], "minutes": "5", "new_budget": "1", "grp": ""},
        follow_redirects=False,
    )
    session_id = int(started.headers["location"].rsplit("/", 1)[-1])

    guard = 0
    while guard < 40:
        guard += 1
        state = client.get(f"/api/sessions/{session_id}").json()
        if state["done"]:
            break
        payload = state["next"]
        if payload["mode"] == "study":
            body = {"item_id": payload["item_id"], "ack": True}
        else:
            with db.connect(settings.db_path) as conn:
                item = sessions_repo.item(conn, int(payload["item_id"]))
                target = trainer.target_person(conn, item)
            body = {"item_id": payload["item_id"], "person_id": int(target["id"]), "latency_ms": 1200}
        client.post(f"/api/sessions/{session_id}/answer", json=body)

    summary = client.get(f"/train/{session_id}/summary")
    assert summary.status_code == 200
    assert "Session summary" in summary.text
    assert "accuracy" in summary.text


def test_a_session_that_does_not_exist_is_a_404_not_a_crash(client, settings):
    seed(settings)
    assert client.get("/train/424242").status_code == 404
    assert client.get("/api/sessions/424242").status_code == 404
    assert "No such session" in client.get("/train/424242/summary").text
