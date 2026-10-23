"""The knob registry, as the installer reads it: ansible/helpers/knobs.yml,
loaded and validated by the tree's render_knobs.
"""

from __future__ import annotations

from pathlib import Path

from . import keys, tree


def load() -> dict:
    return tree.module("helpers.render_knobs").load()


def steps(doc: dict) -> list[dict]:
    """The installer's sections, in order."""
    return list(doc.get("gui_steps") or [])


def step_fields(doc: dict, step: str) -> list[dict]:
    """What one section asks for: secrets first, then the values they
    configure, the order the settings page uses too."""
    entries = [*(doc.get("secrets") or []), *(doc.get("config") or [])]
    return [entry for entry in entries if entry.get("step") == step]


def is_secret(doc: dict, key: str) -> bool:
    return any(entry.get("key") == key for entry in doc.get("secrets") or [])


def default_for(entry: dict) -> str:
    """What a field starts filled with.

    From the `.env` default when the knob has one, because that is the same
    value `catena-installer init` writes. A knob with none starts blank, which
    for every one of them is a real answer.
    """
    return str((entry.get("env") or {}).get("default") or "")


def example_for(entry: dict) -> str:
    """What a blank field shows greyed out, never as its value: the registry's
    `env.example`."""
    return str((entry.get("env") or {}).get("example") or "")


def is_required(entry: dict) -> bool:
    """Whether the installer refuses to install without a value. Everything
    else is labelled optional."""
    return bool(entry.get("required"))


def choice_for(entry: dict) -> dict:
    """The labels of the choice the installer puts in front of a field, by
    option (`public`, `private`) and language, from the registry's
    `gui_choice`, or empty. `public` leaves the field empty; `private` asks
    for it."""
    return dict(entry.get("gui_choice") or {})


def options_for(entry: dict) -> list[str]:
    """The values a field is limited to, from its `env.options`, or an empty
    list for free text."""
    return list((entry.get("env") or {}).get("options") or [])


def _written(path: Path) -> str:
    """A path the way a client types it: with `~` for the home directory."""
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


def suggestions_for(entry: dict, ssh_dir: Path | None = None) -> list[str]:
    """Values the installer suggests beside a free-text field, from the named
    source in `gui_suggestions_from`. `ssh_keys` is the pairs already in
    ~/.ssh: the one whose public half the provider installed is among them."""
    if entry.get("gui_suggestions_from") == "ssh_keys":
        return [_written(p) for p in keys.pairs(ssh_dir or keys.SSH_DIR)]
    return []
