"""Installable, and honest about what "offline" means here.

The PWA promises two things: the app can live on a home screen, and the shell does not have to
be re-downloaded. It deliberately does not promise a third thing — offline training — because a
cached answer would be an invented answer.
"""

from __future__ import annotations

import json


def test_the_manifest_is_served_and_everything_it_promises_exists(client):
    response = client.get("/static/manifest.webmanifest")
    assert response.status_code == 200, response.text
    manifest = json.loads(response.text)

    assert manifest["name"].startswith("Kith")
    assert manifest["start_url"] == "/train", "the icon exists to start training"
    assert manifest["display"] == "standalone"
    assert {"192x192", "512x512"} <= {icon["sizes"] for icon in manifest["icons"]}
    assert any(icon.get("purpose") == "maskable" for icon in manifest["icons"]), (
        "a masked icon without a maskable variant is a white square with a small logo on it"
    )
    for icon in manifest["icons"]:
        assert client.get(icon["src"]).status_code == 200, icon["src"]


def test_the_worker_is_served_from_the_root_so_it_can_see_the_whole_app(client):
    response = client.get("/sw.js")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/javascript")
    assert response.headers.get("service-worker-allowed") == "/"


def test_the_worker_caches_the_shell_and_never_the_app(client):
    body = client.get("/sw.js").text
    assert 'url.pathname.startsWith("/static/")' in body, (
        "the only thing worth caching is the shell; the rule has to be visible in the code"
    )
    assert "caches.open" in body


def test_the_favicon_is_a_real_file_and_the_layout_asks_for_the_app(client):
    icon = client.get("/favicon.ico")
    assert icon.status_code == 200, icon.text
    assert icon.content[:4] == b"\x00\x00\x01\x00", "an .ico header, not an HTML error page"

    page = client.get("/cabinet")
    assert page.status_code == 200, page.text
    assert 'rel="manifest"' in page.text
    assert "serviceWorker" in page.text
    assert "apple-touch-icon" in page.text


def test_the_page_offers_a_theme_colour_for_both_schemes(client):
    page = client.get("/cabinet").text
    assert "#f7f6f3" in page and "#16151a" in page, (
        "one theme colour means a white splash on a phone that lives in dark mode"
    )


def test_the_two_files_a_browser_fetches_on_its_own_are_never_gated():
    """An install must not depend on the visitor's session.

    The test suite runs with auth off and the deployed app does not, so this pins the list
    itself: `/sw.js` answered 200 locally and 303 in production, which no test could see —
    and a worker script served as a redirect is rejected outright.
    """
    from app.main import PUBLIC_PATHS

    assert "/sw.js" in PUBLIC_PATHS, "a gated worker script cannot register"
    assert "/favicon.ico" in PUBLIC_PATHS
