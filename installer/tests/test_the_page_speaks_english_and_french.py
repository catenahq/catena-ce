"""The page's own words exist in every language, and the language is chosen in
a known order.

Run: uv run pytest installer/tests/test_the_page_speaks_english_and_french.py
"""
from __future__ import annotations

import string

import pytest

from catena_installer import i18n


def _placeholders(text: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


def test_every_language_has_every_key_with_the_same_placeholders():
    """A key missing in one language is a page that breaks in it, and a
    placeholder one language forgets is a value it silently drops."""
    english = i18n.TEXT["en"]
    for lang in i18n.LANGS:
        assert set(i18n.TEXT[lang]) == set(english), lang
        for key, text in i18n.TEXT[lang].items():
            assert text.strip(), (lang, key)
            assert _placeholders(text) == _placeholders(english[key]), (lang, key)


def test_every_language_names_itself_on_the_switcher():
    assert set(i18n.NAMES) == set(i18n.LANGS)


@pytest.mark.parametrize("query, cookie, accept, expected", [
    ("fr", "en", "en", "fr"),
    ("", "fr", "en", "fr"),
    ("", "", "fr-CA,fr;q=0.9,en;q=0.8", "fr"),
    ("", "", "en;q=0.5,fr;q=0.9", "fr"),
    ("", "", "de-DE,de;q=0.9", "en"),
    ("xx", "yy", "", "en"),
])
def test_the_choice_then_the_browser_then_english(query, cookie, accept, expected):
    assert i18n.resolve(query, cookie, accept) == expected
