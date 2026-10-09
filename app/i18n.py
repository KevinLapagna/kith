"""Every word the app says, in one place per language.

English is the only locale shipped, but nothing user-visible is allowed to live outside this
table: a sentence typed straight into a template is a sentence a translation can never reach.
A locale is a file — `app/strings/<locale>.json` — with the same keys. Adding German means
adding one file, not editing any code.

Keys are namespaced by where the reader meets them (`nav.*`, `train.*`, `js.*`). A missing key
returns the key itself: invisible in a screenshot, unmissable in a test.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

STRINGS_DIR = Path(__file__).resolve().parent.parent / "locales"
DEFAULT_LOCALE = "en"
ACTIVE_LOCALE = DEFAULT_LOCALE


def set_locale(locale: str) -> None:
    """Pin the locale for this process, called once at startup."""
    global ACTIVE_LOCALE
    ACTIVE_LOCALE = locale or DEFAULT_LOCALE


@lru_cache(maxsize=8)
def table(locale: str) -> dict[str, str]:
    """A locale's catalogue, falling back to English key by key.

    A half-finished translation must render, not crash, so a locale can be added one page at
    a time and the untranslated parts stay in English.
    """
    merged: dict[str, str] = {}
    for name in (DEFAULT_LOCALE, locale):
        path = STRINGS_DIR / f"{name}.json"
        if path.exists():
            merged.update(json.loads(path.read_text(encoding="utf-8")))
    return merged


def t(key: str, **fields: object) -> str:
    """One string for the active locale, formatted. An unknown key comes back as itself."""
    text = table(ACTIVE_LOCALE).get(key, key)
    return text.format(**fields) if fields else text


def js_strings() -> dict[str, str]:
    """The slice of the catalogue the trainer's JavaScript needs, emitted into the page."""
    return {k: v for k, v in table(ACTIVE_LOCALE).items() if k.startswith("js.")}
