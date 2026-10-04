"""The installer's own words, in each language its page speaks.

The registry's fields carry their label and help per language; the rest of
the page reads translations/<lang>.yml, one flat map of key to text with
`{name}` placeholders.
"""

from __future__ import annotations

from pathlib import Path

import yaml

LANGS = ("en", "fr")
DEFAULT = "en"
# What the language switcher calls each language, in that language.
NAMES = {"en": "English", "fr": "Français"}

_DIR = Path(__file__).resolve().parent / "translations"
TEXT: dict[str, dict[str, str]] = {
    lang: yaml.safe_load((_DIR / f"{lang}.yml").read_text(encoding="utf-8"))
    for lang in LANGS
}


def text(lang: str, key: str, **values: object) -> str:
    return TEXT[lang][key].format(**values)


def every(key: str, **values: object) -> dict[str, str]:
    """`key` in every language: the shape the registry's texts have."""
    return {lang: text(lang, key, **values) for lang in LANGS}


def resolve(query: str = "", cookie: str = "", accept_language: str = "") -> str:
    """The page's language: `?lang=`, then the cookie the switcher set, then
    the browser's Accept-Language by preference, then English."""
    for choice in (query, cookie):
        if choice in LANGS:
            return choice
    ranked = []
    for order, part in enumerate(accept_language.split(",")):
        tag, _, params = part.strip().partition(";")
        weight = 1.0
        params = params.strip()
        if params.startswith("q="):
            try:
                weight = float(params[2:])
            except ValueError:
                weight = 0.0
        ranked.append((-weight, order, tag.split("-")[0].strip().lower()))
    for _, _, lang in sorted(ranked):
        if lang in LANGS:
            return lang
    return DEFAULT
