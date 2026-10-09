"""No user-visible string may live outside the table.

Two mechanical checks stand in for "every string comes from one table": once tags, Jinja and
embedded script are stripped from a template, nothing but whitespace and punctuation may
survive; and every key any code asks for — and every entry in the catalogue — must match the
other side exactly. A page added next month is covered by the first check by existing.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app import i18n

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = sorted((ROOT / "templates").glob("*.html"))
SOURCES = [*TEMPLATES, *(ROOT / "app").rglob("*.py"), *(ROOT / "static").rglob("*.js")]

STRIP = [
    (re.compile(r"\{#.*?#\}", re.S), " "),
    (re.compile(r"\{%.*?%\}", re.S), " "),
    (re.compile(r"\{\{.*?\}\}", re.S), " "),
    (re.compile(r"<script\b.*?</script>", re.S | re.I), " "),
    (re.compile(r"<style\b.*?</style>", re.S | re.I), " "),
    (re.compile(r"<[^>]*>", re.S), " "),
    (re.compile(r"&[a-zA-Z]+;", re.S), " "),
]
KEY_CALL = re.compile(r"""\bt\(\s*["']([a-z0-9_.]+)["']""")
JSON_KEY = re.compile(r"""KITH_STRINGS(?:\.|\[["'])([a-z0-9_.]+)""")


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.name)
def test_a_template_holds_no_english_of_its_own(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    for pattern, replacement in STRIP:
        text = pattern.sub(replacement, text)
    words = re.findall(r"[A-Za-z]{2,}", text)
    assert not words, f"{path.name} still carries text of its own: {' '.join(words[:8])}"


def _keys_asked_for() -> dict[str, set[str]]:
    """Keys referenced by a call, by file — the sites that actually render a string.

    A key ending in a dot is a family chosen at runtime (`"stats.replace_error." + code`),
    not a key in its own right, so it is left out here.
    """
    found: dict[str, set[str]] = {}
    for path in SOURCES:
        text = path.read_text(encoding="utf-8")
        keys = set(KEY_CALL.findall(text)) | set(JSON_KEY.findall(text))
        keys = {key for key in keys if not key.endswith(".")}
        if keys:
            found[str(path.relative_to(ROOT))] = keys
    return found


def _source_texts() -> list[str]:
    return [path.read_text(encoding="utf-8") for path in SOURCES]


# Chosen at runtime from a code rather than written out literally, so the scan below cannot
# see them. Naming each family here is cheaper than pretending the test covers it, and the
# list is short on purpose: a key that stops being asked for should be noticed and deleted.
DYNAMIC = ("stats.replace_error.", "mode.hint.")


def test_every_key_asked_for_exists() -> None:
    catalogue = set(i18n.table("en"))
    missing = {
        path: sorted(keys - catalogue)
        for path, keys in _keys_asked_for().items()
        if keys - catalogue
    }
    assert not missing, f"asked for and not in the catalogue: {missing}"


def test_every_catalogue_entry_is_asked_for() -> None:
    """Asked for means the key turns up in the source at all. Most are calls, but some live
    in maps and attributes — `label_key="mode.mc4.label"`, the photo and upload result codes
    — and are just as real, so a literal occurrence counts too."""
    called = set().union(*_keys_asked_for().values())
    texts = _source_texts()
    orphans = sorted(
        key
        for key in i18n.table("en")
        if key not in called
        and not key.startswith(DYNAMIC)
        and not any(key in text for text in texts)
    )
    assert not orphans, f"in the catalogue and asked for by nobody: {orphans}"


def test_an_unknown_key_returns_itself_rather_than_nothing() -> None:
    assert i18n.t("nope.not.a.key") == "nope.not.a.key"


def test_the_image_recipe_copies_the_catalogue() -> None:
    """The Containerfile copies by an explicit list, and the catalogue lives in a directory of
    its own. Leave one line out and the image ships an app that renders `cabinet.title` to a
    real visitor — while every test in this file passes, because on disk the file is present.
    That is how this exact bug reached production once; this test is the cure."""
    recipe = (ROOT / "Containerfile").read_text(encoding="utf-8")
    assert "COPY locales/" in recipe, "the image would ship without its words"


def test_a_missing_catalogue_stops_the_app_at_boot(monkeypatch) -> None:
    """If the catalogue does go missing, the failure must be a refusal to start rather than a
    page full of key names. Deleting the table and building the app proves which it is."""
    monkeypatch.setattr(i18n, "table", lambda locale="en": {})
    from app.main import create_app

    with pytest.raises(RuntimeError):
        create_app()


def test_a_half_finished_locale_renders_anyway() -> None:
    """The fallback is per key, not per file: a locale is added a page at a time."""
    path = i18n.STRINGS_DIR / "zz.json"
    path.write_text(json.dumps({"nav.cabinet": "Kabinett"}), encoding="utf-8")
    i18n.table.cache_clear()
    i18n.set_locale("zz")
    try:
        assert i18n.t("nav.cabinet") == "Kabinett"
        assert i18n.t("nav.groups") == i18n.table("en")["nav.groups"]
    finally:
        i18n.set_locale("en")
        i18n.table.cache_clear()
        path.unlink()


def test_german_is_a_drop_in_not_a_refactor(client) -> None:
    """Two keys in one new file and the interface starts speaking German, while everything
    left untranslated keeps its English instead of falling over. That is the whole point of
    the catalogue — and of forbidding a template to hold prose of its own."""
    path = i18n.STRINGS_DIR / "de.json"
    path.write_text(
        json.dumps({"nav.cabinet": "Schrank", "footer.noauth": "Einzelnutzer"},
                   ensure_ascii=False),
        encoding="utf-8",
    )
    i18n.table.cache_clear()
    i18n.set_locale("de")
    try:
        page = client.get("/cabinet").text
        assert "Schrank" in page, "a translated key never reached the page"
        assert "Einzelnutzer" in page
        assert "The cabinet is empty" in page, "untranslated keys must keep their English"
        for key in ("nav.cabinet", "nav.signout", "cabinet.title", "footer.build"):
            assert key not in page, f"a key name leaked into the page: {key}"
    finally:
        i18n.set_locale("en")
        i18n.table.cache_clear()
        path.unlink()
