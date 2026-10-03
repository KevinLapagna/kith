"""The cabinet end to end: people, photos, groups, search, and the login gate.

These tests run through the same code path the container uses — `create_app` with a
temporary database and the real migrations — so a passing suite means the HTTP surface
was genuinely exercised, not mocked.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import auth, config, db
from app.main import create_app
from app.repo import people as people_repo


def make_image(width: int = 900, height: int = 600, *, colour=(180, 120, 60), fmt: str = "JPEG") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(buffer, format=fmt)
    return buffer.getvalue()


def make_gradient(width: int = 512, height: int = 512) -> bytes:
    """An image with actual structure, so its difference hash is not degenerate."""
    image = Image.new("L", (width, height))
    image.putdata([(x * 7 + y * 3) % 256 for y in range(height) for x in range(width)])
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def add_person(client: TestClient):
    def _add(name: str, **fields) -> int:
        data = {"display_name": name, **fields}
        response = client.post("/people", data=data, follow_redirects=False)
        assert response.status_code == 303, response.text
        return int(response.headers["location"].rsplit("/", 1)[-1])

    return _add


# ── people ───────────────────────────────────────────────────────────────────
def test_cabinet_starts_empty(client: TestClient) -> None:
    response = client.get("/cabinet")
    assert response.status_code == 200
    assert "The cabinet is empty" in response.text
    assert "Add the first person" in response.text


def test_add_person_and_see_them(client: TestClient, add_person) -> None:
    person_id = add_person("Nadia Keller", nickname="Nadi", where_met="Boulder gym", status="active")
    cabinet = client.get("/cabinet")
    assert "Nadia Keller" in cabinet.text

    detail = client.get(f"/people/{person_id}")
    assert detail.status_code == 200
    assert "no photo" in detail.text.lower()


def test_person_requires_a_name(client: TestClient) -> None:
    """The "ask for" box is optional now — but a person with no name at all is still refused."""
    response = client.post("/people", data={"display_name": "   ", "given_name": "  "})
    assert response.status_code == 400
    assert "A first name is required" in response.text


def test_the_first_name_alone_is_enough_to_add_someone(client: TestClient, add_person, settings) -> None:
    """You type the name once. The "ask for" box stays empty and the first name is used."""
    person_id = add_person("", given_name="Yana", family_name="Weiss")

    cabinet = client.get("/cabinet")
    assert "Yana" in cabinet.text
    assert "Weiss" in cabinet.text
    assert 'class="name-family"' in cabinet.text, "the family name is shown, in its own element"

    with db.connect(settings.db_path) as conn:
        raw = conn.execute(
            "SELECT display_name, given_name, family_name FROM person WHERE id = ?", (person_id,)
        ).fetchone()
        resolved = people_repo.get(conn, person_id)
    assert raw["display_name"] == "", "the override box is stored empty, not duplicated"
    assert raw["given_name"] == "Yana"
    assert resolved["display_name"] == "Yana", "reads resolve the name to what will be asked"


def test_search_finds_a_person_by_the_first_or_last_name(client: TestClient, add_person) -> None:
    wanted = add_person("", given_name="Priya", family_name="Raman")
    other = add_person("", given_name="Luca", family_name="Rossi")

    by_first = client.get("/cabinet?q=priya")
    assert f'href="/people/{wanted}"' in by_first.text
    assert f'href="/people/{other}"' not in by_first.text

    by_last = client.get("/cabinet?q=aman")  # mid-word, so the LIKE fallback answers this one
    assert f'href="/people/{wanted}"' in by_last.text
    assert f'href="/people/{other}"' not in by_last.text


def test_a_group_card_shows_the_face_not_a_letter(client: TestClient, add_person, settings) -> None:
    person_id = add_person("", given_name="Yana", family_name="Weiss")
    uploaded = client.post(
        f"/people/{person_id}/photos", files={"photos": ("a.jpg", make_image(), "image/jpeg")}
    )
    assert uploaded.status_code in (200, 303), uploaded.text

    created = client.post("/groups", data={"name": "Padel crew"}, follow_redirects=False)
    group_id = int(created.headers["location"].rsplit("/", 1)[-1])
    client.post(f"/groups/{group_id}", data={"name": "Padel crew", "members": [str(person_id)]})

    with db.connect(settings.db_path) as conn:
        photo_id = int(
            conn.execute("SELECT id FROM photo WHERE person_id = ?", (person_id,)).fetchone()["id"]
        )

    page = client.get(f"/groups/{group_id}")
    assert page.status_code == 200
    assert f"/photos/{photo_id}/thumb.webp" in page.text
    assert 'class="initial"' not in page.text, "the letter placeholder is only for people with no photo"


def test_person_edit_round_trip(client: TestClient, add_person) -> None:
    person_id = add_person("Ana Weber")
    response = client.post(
        f"/people/{person_id}",
        data={"display_name": "Ana Weber", "nickname": "Anu", "status": "paused"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    detail = client.get(f"/people/{person_id}")
    assert "Anu" in detail.text
    assert "Ana Weber" in client.get("/cabinet?status=paused").text
    # ...and the default filter (active) leaves a paused person out.
    assert "Ana Weber" not in client.get("/cabinet").text


def test_status_filter_hides_archived(client: TestClient, add_person) -> None:
    add_person("Visible Person")
    add_person("Hidden Person", status="archived")
    assert "Visible Person" in client.get("/cabinet").text
    assert "Hidden Person" not in client.get("/cabinet").text
    assert "Hidden Person" in client.get("/cabinet?status=archived").text


def test_delete_person_removes_them(client: TestClient, add_person) -> None:
    person_id = add_person("Temporary Person")
    response = client.post(f"/people/{person_id}/delete", follow_redirects=False)
    assert response.status_code == 303
    assert client.get(f"/people/{person_id}").status_code == 404
    assert "Temporary Person" not in client.get("/cabinet").text


def test_unknown_person_is_a_friendly_404(client: TestClient) -> None:
    response = client.get("/people/424242")
    assert response.status_code == 404
    assert "Not found" in response.text


# ── search ───────────────────────────────────────────────────────────────────
def test_search_finds_by_name_fragment(client: TestClient, add_person) -> None:
    add_person("Kumar Rajan")
    add_person("Beatrice Ott")
    assert "Kumar Rajan" in client.get("/cabinet?q=kum").text
    assert "Kumar Rajan" not in client.get("/cabinet?q=beatrice").text


def test_cabinet_exposes_a_live_search_region(client: TestClient, add_person) -> None:
    """Typing filters in place (static/js/live-search.js swaps #results).

    Both halves are load-bearing: `id="results"` is the contract with the script, and the
    script has to be *served* — a 404 would silently leave the cabinet untyped, with no
    error anywhere. (Static assets are exempt from the login gate, or the page it is meant
    to enhance could not load it.)
    """
    add_person("Rafa Nadal")
    page = client.get("/cabinet")
    assert page.status_code == 200
    assert 'id="results"' in page.text
    assert "live-search.js" in page.text
    assert client.get("/static/js/live-search.js").status_code == 200


def test_search_is_diacritic_insensitive(client: TestClient, add_person) -> None:
    add_person("Jürg Müller")
    assert "Jürg Müller" in client.get("/cabinet?q=muller").text


def test_search_reaches_notes_and_mnemonic(client: TestClient, add_person) -> None:
    add_person("Silent Person", notes="plays the double bass", mnemonic="bass clef")
    assert "Silent Person" in client.get("/cabinet?q=double bass").text
    assert "Silent Person" in client.get("/cabinet?q=clef").text


def test_search_works_when_the_form_submits_empty_filters(client: TestClient, add_person) -> None:
    """Regression: search from the UI returned 422 and found nothing.

    The toolbar sends *all* of its fields at once, so an unselected group arrives as
    `group=`. Declared `int | None`, that empty string failed parsing before the search
    ran. Submit exactly what the browser submits, not a hand-picked query string.
    """
    add_person("Rafa Nadal")
    response = client.get("/cabinet", params={"q": "raf", "group": "", "status": "active"})
    assert response.status_code == 200, response.text
    assert "Rafa Nadal" in response.text

    # A numeric group id that matches nothing is a legitimate empty result, not an error —
    # and it must *stay* empty: the mid-word LIKE fallback used to rebuild the query
    # without the filters, so a search combined with a group listed people from outside it.
    empty = client.get("/cabinet", params={"q": "raf", "group": "424242", "status": ""})
    assert empty.status_code == 200, empty.text
    assert "Rafa Nadal" not in empty.text
    # …and a value that is not a number at all is ignored rather than fatal.
    garbage = client.get("/cabinet", params={"q": "raf", "group": "not-a-number"})
    assert garbage.status_code == 200, garbage.text
    assert "Rafa Nadal" in garbage.text


def test_search_mid_word_falls_back_to_like(client: TestClient, add_person) -> None:
    add_person("Ana Weber")
    # "ebe" is not a word prefix, so only the LIKE fallback can find it.
    assert "Ana Weber" in client.get("/cabinet?q=ebe").text


def test_search_ignores_fts_syntax(client: TestClient, add_person) -> None:
    add_person("Someone Real")
    response = client.get('/cabinet?q=NEAR(" OR NOT ')
    assert response.status_code == 200  # must not blow up on FTS5 operator characters


# ── photos ───────────────────────────────────────────────────────────────────
def test_upload_photo_stores_two_renditions(client: TestClient, add_person) -> None:
    person_id = add_person("Photo Person")
    response = client.post(
        f"/people/{person_id}/photos",
        files={"photos": ("face.jpg", make_image(2400, 1600), "image/jpeg")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "photos_added_1" in response.headers["location"]

    detail = client.get(f"/people/{person_id}")
    assert "primary" in detail.text

    with client.app.state.settings.db_path.open("rb") as handle:  # type: ignore[attr-defined]
        assert handle.read(16).startswith(b"SQLite format 3")

    # Both renditions are served, and the stored one is downscaled to the 1024px edge.
    import sqlite3

    conn = sqlite3.connect(str(client.app.state.settings.db_path))  # type: ignore[attr-defined]
    row = conn.execute("SELECT id, width, height, length(data), length(thumb), mime FROM photo").fetchone()
    conn.close()
    photo_id, width, height, data_len, thumb_len, mime = row
    assert width == 1024 and height in (682, 683)
    assert data_len > 0 and thumb_len > 0
    assert mime == "image/webp"

    assert client.get(f"/photos/{photo_id}.webp").headers["content-type"] == "image/webp"
    assert client.get(f"/photos/{photo_id}/thumb.webp").status_code == 200


def test_duplicate_upload_is_rejected(client: TestClient, add_person) -> None:
    person_id = add_person("Dup Person")
    payload = make_image(700, 700)
    client.post(f"/people/{person_id}/photos", files={"photos": ("a.jpg", payload, "image/jpeg")})
    response = client.post(
        f"/people/{person_id}/photos",
        files={"photos": ("b.jpg", payload, "image/jpeg")},
        follow_redirects=False,
    )
    assert "err=duplicate" in response.headers["location"]


def test_non_image_upload_is_rejected_politely(client: TestClient, add_person) -> None:
    person_id = add_person("Text Person")
    response = client.post(
        f"/people/{person_id}/photos",
        files={"photos": ("notes.txt", b"this is not an image", "text/plain")},
        follow_redirects=False,
    )
    assert "err=not_an_image" in response.headers["location"]


def test_oversized_upload_is_rejected(client: TestClient, add_person) -> None:
    add_person("Big Person")
    # Settings is frozen, so build a small-cap app rather than mutating the fixture's.
    settings = config.Settings(
        data_dir=client.app.state.settings.data_dir,  # type: ignore[attr-defined]
        db_path=client.app.state.settings.db_path,  # type: ignore[attr-defined]
        max_upload_mb=0,
        log_level="warning",
    )
    with TestClient(create_app(settings)) as small:
        small.post("/people", data={"display_name": "Big Person"})
        response = small.post(
            "/people/1/photos",
            files={"photos": ("big.jpg", make_image(400, 400), "image/jpeg")},
            follow_redirects=False,
        )
    assert "err=too_large" in response.headers["location"]


def test_photo_etag_returns_304(client: TestClient, add_person) -> None:
    person_id = add_person("Etag Person")
    client.post(f"/people/{person_id}/photos", files={"photos": ("f.jpg", make_image(), "image/jpeg")})
    first = client.get("/photos/1.webp")
    etag = first.headers["etag"]
    assert "immutable" in first.headers["cache-control"]
    second = client.get("/photos/1.webp", headers={"if-none-match": etag})
    assert second.status_code == 304


def test_deleting_primary_promotes_the_next_photo(client: TestClient, add_person) -> None:
    person_id = add_person("Two Photos")
    client.post(
        f"/people/{person_id}/photos",
        files=[("photos", ("one.jpg", make_image(colour=(10, 10, 10)), "image/jpeg")),
               ("photos", ("two.jpg", make_image(colour=(200, 200, 200)), "image/jpeg"))],
    )
    assert client.post(f"/people/{person_id}/photos/1/delete", follow_redirects=False).status_code == 303
    detail = client.get(f"/people/{person_id}")
    assert detail.status_code == 200
    assert "primary" in detail.text  # photo 2 was promoted, so a primary still exists


def test_exif_orientation_is_applied_and_metadata_dropped(client: TestClient, add_person) -> None:
    """A rotated JPEG must come out upright, and must not carry its EXIF through."""
    person_id = add_person("Rotated Person")
    image = Image.new("RGB", (400, 800), (30, 140, 90))
    exif = image.getexif()
    exif[274] = 6  # Orientation: rotate 90° CW
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", exif=exif)
    payload = buffer.getvalue()

    client.post(f"/people/{person_id}/photos", files={"photos": ("rot.jpg", payload, "image/jpeg")})

    import sqlite3

    conn = sqlite3.connect(str(client.app.state.settings.db_path))  # type: ignore[attr-defined]
    blob = conn.execute("SELECT data FROM photo").fetchone()[0]
    conn.close()
    with Image.open(io.BytesIO(blob)) as stored:
        assert stored.size == (800, 400)          # transposed
        assert not stored.getexif()                # and nothing carried over


def test_photo_serving_requires_a_session_when_auth_is_on() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        settings = config.Settings(
            data_dir=Path(tmp),
            db_path=Path(tmp) / "kith.sqlite",
            auth_enabled=True,
            password_hash=auth.hash_password("correct-horse-battery"),
            session_secret="test-secret",
            log_level="warning",
        )
        with TestClient(create_app(settings)) as locked:
            assert locked.get("/photos/1.webp", follow_redirects=False).status_code == 303
            assert locked.get("/photos/1/thumb.webp", follow_redirects=False).status_code == 303


# ── groups ───────────────────────────────────────────────────────────────────
def test_group_crud_and_membership(client: TestClient, add_person) -> None:
    first = add_person("Group One")
    second = add_person("Group Two")

    created = client.post("/groups", data={"name": "Padel crew", "note": "Tuesdays"},
                          follow_redirects=False)
    assert created.status_code == 303
    group_id = int(created.headers["location"].rsplit("/", 1)[-1])

    client.post(f"/groups/{group_id}", data={"name": "Padel crew", "members": [str(first)]})
    listing = client.get("/groups")
    assert "Padel crew" in listing.text

    detail = client.get(f"/groups/{group_id}")
    assert "Group One" in detail.text
    # Membership is a difference update: adding the second person keeps the first.
    client.post(f"/groups/{group_id}", data={"name": "Padel crew", "members": [str(first), str(second)]})
    assert "2 people" in client.get(f"/groups/{group_id}").text

    client.post(f"/groups/{group_id}", data={"name": "Padel crew"})
    assert "0 people" in client.get(f"/groups/{group_id}").text

    assert client.post(f"/groups/{group_id}/delete", follow_redirects=False).status_code == 303
    assert "Padel crew" not in client.get("/groups").text


def test_duplicate_group_name_is_rejected(client: TestClient) -> None:
    client.post("/groups", data={"name": "Twice"})
    response = client.post("/groups", data={"name": "Twice"})
    assert response.status_code == 400
    assert "needs a name, and it must be unique" in response.text


def test_group_filter_on_cabinet(client: TestClient, add_person) -> None:
    inside = add_person("In The Group")
    add_person("Not In The Group")
    client.post("/groups", data={"name": "Only Some"})
    client.post("/groups/1", data={"name": "Only Some", "members": [str(inside)]})

    filtered = client.get("/cabinet?group=1")
    assert "In The Group" in filtered.text
    assert "Not In The Group" not in filtered.text


def test_person_can_be_in_several_groups(client: TestClient, add_person) -> None:
    person_id = add_person("Overlapping Person")
    client.post("/groups", data={"name": "Alpha"})
    client.post("/groups", data={"name": "Beta"})
    client.post(f"/people/{person_id}", data={"display_name": "Overlapping Person",
                                              "groups": ["1", "2"]})
    detail = client.get(f"/people/{person_id}")
    assert "Alpha" in detail.text and "Beta" in detail.text


def test_deleting_a_person_cleans_up_membership(client: TestClient, add_person) -> None:
    person_id = add_person("Soon Gone")
    client.post("/groups", data={"name": "Survivors"})
    client.post("/groups/1", data={"name": "Survivors", "members": [str(person_id)]})
    client.post(f"/people/{person_id}/delete")
    assert "0 people" in client.get("/groups/1").text


# ── login gate ───────────────────────────────────────────────────────────────
PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
def locked_client(tmp_path: Path):
    settings = config.Settings(
        data_dir=tmp_path,
        db_path=tmp_path / "kith.sqlite",
        auth_enabled=True,
        password_hash=auth.hash_password(PASSWORD),
        session_secret="a-test-secret",
        session_days=1,
        log_level="warning",
    )
    with TestClient(create_app(settings)) as client:
        yield client


def test_pages_redirect_to_login_when_locked(locked_client: TestClient) -> None:
    for path in ("/", "/cabinet", "/groups", "/people/new"):
        response = locked_client.get(path, follow_redirects=False)
        assert response.status_code == 303, path
        assert response.headers["location"].startswith("/login"), path


def test_health_stays_public_but_loses_the_details(locked_client: TestClient) -> None:
    response = locked_client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    # The whole point: a public endpoint must not describe the filesystem.
    assert "db_path" not in body
    assert "data_dir" not in body
    assert "people" not in body


def test_health_shows_everything_with_a_session(locked_client: TestClient) -> None:
    login = locked_client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    assert login.status_code == 303
    body = locked_client.get("/api/health").json()
    assert "db_path" in body and "data_dir" in body


def test_wrong_password_is_refused(locked_client: TestClient) -> None:
    response = locked_client.post("/login", data={"password": "nope"})
    assert response.status_code == 401
    assert "not accepted" in response.text
    assert "kith_session" not in response.headers.get("set-cookie", "")


def test_login_sets_an_httponly_cookie_then_grants_access(locked_client: TestClient) -> None:
    response = locked_client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=Lax" in cookie
    assert "Secure" not in cookie           # plain-HTTP loopback must still work
    assert locked_client.get("/cabinet").status_code == 200


def test_logout_clears_the_session(locked_client: TestClient) -> None:
    locked_client.post("/login", data={"password": PASSWORD})
    locked_client.get("/logout")
    assert locked_client.get("/cabinet", follow_redirects=False).status_code == 303


def test_api_paths_answer_401_not_a_login_page(locked_client: TestClient) -> None:
    """An /api/ path must never be handed an HTML login page: a fetch should be able to
    tell "not signed in" from "signed in but something went wrong"."""
    response = locked_client.get("/api/nothing-here", follow_redirects=False)
    assert response.status_code == 401
    assert response.json() == {"ok": False, "error": "authentication required"}


def test_forged_cookie_is_rejected(locked_client: TestClient) -> None:
    locked_client.cookies.set("kith_session", "not-a-real-token")
    assert locked_client.get("/cabinet", follow_redirects=False).status_code == 303


def test_expired_token_is_rejected() -> None:
    assert auth.verify_token("secret", auth.issue_token("secret", days=-1)) is False


def test_login_next_cannot_leave_the_site(locked_client: TestClient) -> None:
    response = locked_client.post(
        "/login", data={"password": PASSWORD, "next": "//evil.example.com"}, follow_redirects=False
    )
    assert response.headers["location"] == "/"


def test_boot_refuses_auth_without_a_hash(tmp_path: Path) -> None:
    settings = config.Settings(
        data_dir=tmp_path,
        db_path=tmp_path / "kith.sqlite",
        auth_enabled=True,
        password_hash=None,
        session_secret="x",
        log_level="warning",
    )
    with pytest.raises(RuntimeError, match="KITH_PASSWORD_HASH is empty"):
        with TestClient(create_app(settings)):
            pass


def test_database_survives_a_restart(tmp_path: Path) -> None:
    settings = config.Settings(
        data_dir=tmp_path, db_path=tmp_path / "kith.sqlite", log_level="warning"
    )
    with TestClient(create_app(settings)) as first:
        first.post("/people", data={"display_name": "Persistent Person"})
    with TestClient(create_app(settings)) as second:
        assert "Persistent Person" in second.get("/cabinet").text
