"""Statistics: the two definitions, and the arithmetic that must not drift.

These tests pin *meanings*, not numbers — what counts as "known", what a quiet day does to
the series, which reviews the window includes, and that the calibration view stays silent
until you have said something about yourself. A stats page is only as honest as these
queries, and it is the one screen where a plausible-looking lie would go unnoticed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import db
from app.repo import stats


def stamp(days_ago: int) -> str:
    """A timestamp in the app's own format, whole days back from now."""
    moment = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return moment.replace(hour=12, minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_person(conn, name: str, *, status: str = "active", photo: bool = True) -> int:
    now = stamp(0)
    cursor = conn.execute(
        "INSERT INTO person (display_name, given_name, family_name, status, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (name, name, None, status, now, now),
    )
    person_id = int(cursor.lastrowid or 0)
    if photo:
        add_photo(conn, person_id)
    return person_id


def add_photo(conn, person_id: int, *, primary: int = 1, size: int = 4096) -> None:
    conn.execute(
        "INSERT INTO photo (person_id, is_primary, mime, width, height, bytes, sha256, data,"
        " thumb, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (person_id, primary, "image/webp", 8, 8, size, f"sha-{person_id}-{primary}", b"", b"",
         stamp(0)),
    )


def add_card(
    conn,
    person_id: int,
    *,
    direction: str = "face_to_name",
    stability: float | None = 1.0,
    difficulty: float = 5.0,
    reps: int = 1,
    lapses: int = 0,
    suspended: int = 0,
) -> int:
    now = stamp(0)
    cursor = conn.execute(
        "INSERT INTO card (person_id, direction, fsrs, due, stability, difficulty, state, reps,"
        " lapses, suspended, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (person_id, direction, "{}", stamp(1), stability, difficulty, 2, reps, lapses,
         suspended, now, now),
    )
    return int(cursor.lastrowid or 0)


def add_review(
    conn,
    card_id: int,
    *,
    correct: bool = True,
    confidence: int | None = None,
    mode: str = "mc4",
    days_ago: int = 0,
    chosen_person_id: int | None = None,
    latency_ms: int | None = None,
) -> None:
    conn.execute(
        "INSERT INTO review (card_id, reviewed_at, rating, mode, correct, confidence,"
        " chosen_person_id, latency_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (card_id, stamp(days_ago), 3 if correct else 1, mode, 1 if correct else 0, confidence,
         chosen_person_id, latency_ms),
    )


# ── the two definitions ─────────────────────────────────────────────────────
def test_strength_is_monotone_saturating_and_never_negative(settings):
    """Strength must never lie upward: a fresh card is 0, an old one approaches 100."""
    assert stats.strength(None) == 0
    assert stats.strength(0) == 0
    ladder = [stats.strength(days) for days in (1, 5, 10, 20, 45, 90, 365, 3650)]
    assert ladder == sorted(ladder), ladder
    assert all(0 <= value <= 100 for value in ladder), ladder
    assert ladder[-1] == 100 and ladder[0] < 10


def test_the_known_line_sits_where_the_documentation_says(settings):
    """`known` is defined as strength 60, and the stability behind it is derived, not typed."""
    assert stats.KNOWN_STRENGTH == 60
    assert stats.strength(stats.KNOWN_STABILITY) == 60
    # Just below the line is not known; comfortably above it is.
    assert stats.strength(stats.KNOWN_STABILITY - 1) == 59


# ── the series and the window ───────────────────────────────────────────────
def test_the_series_keeps_quiet_days_and_starts_oldest_first(settings):
    """A gap is information: a day with no answers must not vanish from the axis."""
    with db.connect(settings.db_path) as conn:
        db.migrate(conn)
        person_id = make_person(conn, "Anna")
        card_id = add_card(conn, person_id)
        add_review(conn, card_id, days_ago=0)
        add_review(conn, card_id, correct=False, days_ago=3)

        series = stats.daily_reviews(conn, days=7)

    assert len(series) == 7, series
    assert [day["day"] for day in series] == sorted(day["day"] for day in series)
    assert series[-1]["day"].endswith(series[-1]["day"])  # today is last
    counts = {day["day"]: day["answered"] for day in series}
    assert sum(counts.values()) == 2, counts
    assert list(counts.values())[-2] == 0, "the quiet day is still on the axis"


def test_retention_counts_only_reviews_inside_the_window(settings):
    """An old wrong answer must not drag down today's retention estimate."""
    with db.connect(settings.db_path) as conn:
        db.migrate(conn)
        card_id = add_card(conn, make_person(conn, "Anna"))
        add_review(conn, card_id, days_ago=1)
        add_review(conn, card_id, days_ago=2)
        add_review(conn, card_id, correct=False, days_ago=90)

        retention = stats.retention(conn, days=30)

    assert retention["answered"] == 2, retention
    assert retention["correct"] == 2, retention
    assert retention["rate"] == 1.0, retention
    assert retention["active_days"] == 2, retention


def test_retention_is_undefined_rather_than_zero_when_nothing_was_answered(settings):
    """Nothing to report is not the same as failing. `None` prints as a dash, 0.0 as a lie."""
    with db.connect(settings.db_path) as conn:
        db.migrate(conn)
        retention = stats.retention(conn, days=30)

    assert retention["answered"] == 0
    assert retention["rate"] is None, retention


# ── what counts as known ────────────────────────────────────────────────────
def test_known_needs_a_reviewed_card_at_the_line_and_a_photo(settings):
    """Four distinctions: solid vs thin, suspended, answered at all, and no photo to ask with."""
    with db.connect(settings.db_path) as conn:
        db.migrate(conn)
        solid = add_card(conn, make_person(conn, "Solid"), stability=stats.KNOWN_STABILITY + 20)
        thin = add_card(conn, make_person(conn, "Thin"), stability=10)
        suspended = add_card(
            conn,
            make_person(conn, "Snoozed"),
            stability=200,
            suspended=1,
        )
        unseen = add_card(conn, make_person(conn, "Unseen"), stability=0, reps=0)
        add_card(  # the wrong direction does not count: you must be able to name the *face*
            conn, make_person(conn, "Backwards"), direction="name_to_face", stability=300
        )
        make_person(conn, "NoPhoto", photo=False)
        add_review(conn, solid)
        add_review(conn, thin)
        add_review(conn, unseen, correct=False)

        knowledge = stats.knowledge(conn)

    # Trainable = active people with a photo: every person above except NoPhoto.
    assert knowledge["trainable"] == 5, knowledge
    assert knowledge["known"] == 1, knowledge
    assert knowledge["suspended"] == 1, knowledge
    assert knowledge["people"] == 6, knowledge


def test_difficulty_spreads_over_buckets_and_skips_unanswered_cards(settings):
    """Difficulty means nothing until the card has been answered; it must not pile up at 5."""
    with db.connect(settings.db_path) as conn:
        db.migrate(conn)
        add_card(conn, make_person(conn, "Easy"), difficulty=1.5)
        add_card(conn, make_person(conn, "Hard"), difficulty=9.5)
        add_card(conn, make_person(conn, "Also hard"), difficulty=7.0)
        add_card(conn, make_person(conn, "Untouched"), difficulty=5.0, reps=0)
        add_card(conn, make_person(conn, "Snoozed"), difficulty=5.0, suspended=1)

        histogram = stats.difficulty_histogram(conn)

    buckets = {(bucket["low"], bucket["high"]): bucket["cards"] for bucket in histogram}
    assert buckets[(1, 2)] == 1, buckets
    assert buckets[(7, 8)] == 1, buckets
    assert buckets[(9, 10)] == 1, buckets
    assert sum(buckets.values()) == 3, buckets


# ── groups ──────────────────────────────────────────────────────────────────
def test_group_coverage_ignores_paused_members_and_names_the_weakest(settings):
    """A paused person is not a gap in your knowledge, and the weakest list must be useful."""
    with db.connect(settings.db_path) as conn:
        db.migrate(conn)
        known = make_person(conn, "Known")
        shaky = make_person(conn, "Shaky")
        stranger = make_person(conn, "Stranger")
        paused = make_person(conn, "Paused", status="paused")
        conn.execute("INSERT INTO grp (name, created_at) VALUES ('Work', ?)", (stamp(0),))
        group_id = int(conn.execute("SELECT id FROM grp WHERE name = 'Work'").fetchone()["id"])
        for person_id in (known, shaky, stranger, paused):
            conn.execute(
                "INSERT INTO grp_member (grp_id, person_id, added_at) VALUES (?, ?, ?)",
                (group_id, person_id, stamp(0)),
            )
        add_card(conn, known, stability=stats.KNOWN_STABILITY + 30)
        add_card(conn, shaky, stability=2)
        # `stranger` has no card at all.

        groups = stats.group_coverage(conn)

    assert len(groups) == 1, groups
    work = groups[0]
    assert work["name"] == "Work"
    assert work["members"] == 3, work  # the paused member is not counted
    assert work["known"] == 1 and work["unknown"] == 2, work
    assert work["rate"] == 1 / 3, work
    assert work["due"] == 2, work  # two members have a card due; the stranger has none
    names = [entry["display_name"] for entry in work["weakest"]]
    assert names[0] == "Stranger", names  # no card at all is weaker than a thin one
    assert "Paused" not in names, names


# ── calibration ─────────────────────────────────────────────────────────────
def test_calibration_says_nothing_until_you_rate_yourself(settings):
    """Months of unrated reviews must not render as a perfect calibration."""
    with db.connect(settings.db_path) as conn:
        db.migrate(conn)
        card_id = add_card(conn, make_person(conn, "Anna"))
        add_review(conn, card_id)
        add_review(conn, card_id, correct=False)

        calibration = stats.calibration(conn)

    assert calibration["rated"] == 0, calibration
    assert calibration["sure_rate"] is None and calibration["unsure_rate"] is None, calibration
    assert calibration["separation"] is None, calibration
    assert calibration["worst"] == [], calibration


def test_calibration_measures_the_gap_between_feeling_and_fact(settings):
    """The one number that earns its place: how much more often you are right when sure."""
    with db.connect(settings.db_path) as conn:
        db.migrate(conn)
        anna = make_person(conn, "Anna")
        bahar = make_person(conn, "Bahar")
        card_id = add_card(conn, anna)
        add_review(conn, card_id, confidence=2)  # sure, right
        add_review(conn, card_id, correct=False, confidence=2)  # sure, wrong
        add_review(conn, card_id, correct=False, confidence=2)  # sure, wrong again
        add_review(conn, card_id, confidence=1)  # unsure, right

        calibration = stats.calibration(conn)

    assert calibration["rated"] == 4, calibration
    assert calibration["sure_right"] == 1 and calibration["sure_wrong"] == 2, calibration
    assert calibration["unsure_right"] == 1 and calibration["unsure_wrong"] == 0, calibration
    assert calibration["sure_rate"] == 1 / 3, calibration
    assert calibration["unsure_rate"] == 1.0, calibration
    # Negative separation is the finding, not a bug: certainty was anti-informative here.
    assert calibration["separation"] == (1 / 3) - 1.0, calibration
    assert [entry["display_name"] for entry in calibration["worst"]] == ["Anna"], calibration
    assert calibration["worst"][0]["times"] == 2, calibration


# ── storage and one person ──────────────────────────────────────────────────
def test_storage_reports_the_real_file_and_the_photo_share(settings):
    """The single-file promise: the page must show the bytes that actually exist on disk."""
    with db.connect(settings.db_path) as conn:
        db.migrate(conn)
        person_id = make_person(conn, "Anna")
        conn.execute("UPDATE photo SET bytes = 1000 WHERE person_id = ?", (person_id,))
        conn.execute("UPDATE photo SET bytes = 3000 WHERE id = (SELECT max(id) FROM photo)")
        add_photo(conn, person_id, primary=0, size=6000)

        storage = stats.storage(conn, settings.db_path)

    assert storage["db"] > 0, storage
    assert storage["total"] >= storage["db"], storage
    assert storage["photos"] == 2, storage
    assert storage["photo_bytes"] == 9000, storage
    assert storage["per_photo"] == 4500, storage
    assert storage["path"] == str(settings.db_path), storage


def test_person_stats_reads_a_confusion_from_both_sides(settings):
    """Mixing A up with B is one event and two facts, and both belong on the pages."""
    with db.connect(settings.db_path) as conn:
        db.migrate(conn)
        anna = make_person(conn, "Anna")
        bahar = make_person(conn, "Bahar")
        anna_card = add_card(conn, anna)
        bahar_card = add_card(conn, bahar)
        # Anna's face was given Bahar's name, twice; and once the other way round.
        add_review(conn, anna_card, correct=False, chosen_person_id=bahar, latency_ms=2000)
        add_review(conn, anna_card, correct=False, chosen_person_id=bahar, latency_ms=4000)
        add_review(conn, bahar_card, correct=False, chosen_person_id=anna)
        add_review(conn, anna_card, latency_ms=3000)

        anna_stats = stats.person_stats(conn, anna)
        bahar_stats = stats.person_stats(conn, bahar)

    assert anna_stats["answered"] == 3, anna_stats
    assert anna_stats["correct"] == 1, anna_stats
    assert anna_stats["rate"] == 1 / 3, anna_stats
    assert anna_stats["mean_latency_ms"] == 3000, anna_stats  # (2000+4000+3000)/3
    assert [(row["other_name"], row["times"]) for row in anna_stats["confused_with"]] == [
        ("Bahar", 2)
    ], anna_stats
    assert [(row["other_name"], row["times"]) for row in bahar_stats["mistaken_for"]] == [
        ("Anna", 2)
    ], bahar_stats
    assert bahar_stats["confused_with"] == [{"other_name": "Anna", "times": 1}], bahar_stats


# ── the page itself ─────────────────────────────────────────────────────────
def test_the_progress_page_admits_an_empty_cabinet(client):
    """The likeliest early state: nothing yet. It must say so rather than print 0 %."""
    response = client.get("/stats")

    assert response.status_code == 200, response.text
    assert "Nothing to train on yet" in response.text, response.text
    assert "Nothing answered yet" in response.text
    assert "Nothing has been swapped yet" in response.text
    assert "have not told Kith how sure you were yet" in response.text
    assert "No groups yet" in response.text


def test_the_progress_page_reports_the_cabinet_it_has(client, settings):
    """With data, the page names people and states the calibration it can actually see."""
    with db.connect(settings.db_path) as conn:
        db.migrate(conn)
        anna = make_person(conn, "Anna")
        bahar = make_person(conn, "Bahar")
        card = add_card(conn, anna, stability=stats.KNOWN_STABILITY + 10)
        add_review(conn, card, confidence=2)
        add_review(conn, card, correct=False, confidence=2, chosen_person_id=bahar)

    response = client.get("/stats")

    assert response.status_code == 200, response.text
    assert "You can name" in response.text
    assert "Anna" in response.text and "Bahar" in response.text
    assert "1 mix-up" in response.text, response.text
    assert "When you were sure" in response.text
    assert "50 %" in response.text, "one of two confident answers was right"
