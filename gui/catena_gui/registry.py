"""The knob registry, as the launcher reads it.

ONE DECLARATION, READ NOT COPIED. `helpers/knobs.json` is the same artifact the
on-box store, the installer and the settings page read. The launcher renders
its pages from it and holds no list of its own, so a knob added there appears
here and a knob removed there disappears -- which is the only version of "the
installer and the server agree" that survives a year of edits.

RESOLVED, NOT PACKAGED. The registry lives in the sibling `ansible/` tree
rather than inside this package, because `vendor-catena-ce.sh` copies
`git ls-files -- ansible` into the public panel image and the launcher must not
ship there. So it is found by path, and a missing one RAISES: a launcher that
fell back to an empty registry would render six blank pages and then produce an
install.yaml that answered nothing.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

REGISTRY_FILENAME = "knobs.json"

# Where the ansible tree sits relative to this file: gui/catena_gui/ -> gui/ ->
# the repo root. A checkout is the only layout this runs in, because the thing
# it drives (`catena install`) is in that same checkout.
_REPO_ROOT = Path(__file__).resolve().parents[2]
ANSIBLE_DIR = _REPO_ROOT / "ansible"


def registry_path() -> Path:
    """Where the registry is, with an explicit override for a test or an odd
    checkout. The same environment variable the on-box reader honours, so one
    name answers the question wherever it is asked."""
    explicit = os.environ.get("CATENA_KNOBS", "").strip()
    candidate = Path(explicit) if explicit else ANSIBLE_DIR / "helpers" / REGISTRY_FILENAME
    if candidate.is_file():
        return candidate
    raise FileNotFoundError(
        f"the knob registry is not at {candidate}. It is rendered from "
        "ansible/helpers/knobs.yml by render_knobs.py, and without it the "
        "launcher has no questions to ask."
    )


def load() -> dict:
    doc = json.loads(registry_path().read_text(encoding="utf-8"))
    if doc.get("version") != 1:
        raise ValueError(
            f"{REGISTRY_FILENAME} declares version {doc.get('version')!r}; "
            "this reader knows version 1"
        )
    return doc


def steps(doc: dict) -> list[dict]:
    """The installer's pages, in order."""
    return list(doc.get("gui_steps") or [])


def step_fields(doc: dict, step: str) -> list[dict]:
    """What one page asks for: secrets first, then the values they configure.

    That order is the settings page's too, so a client who has seen one
    recognises the other.
    """
    entries = [*(doc.get("secrets") or []), *(doc.get("config") or [])]
    return [entry for entry in entries if entry.get("step") == step]


def is_secret(doc: dict, key: str) -> bool:
    return any(entry.get("key") == key for entry in doc.get("secrets") or [])


def default_for(entry: dict) -> str:
    """What a field starts filled with.

    From the `.env` default when the knob has one, because that is the same
    value a client editing the template by hand would see. A knob with none
    starts blank, which for every one of them is a real answer.
    """
    return str((entry.get("env") or {}).get("default") or "")


def options_for(entry: dict) -> list[str]:
    """The values a field accepts, or an empty list for free text.

    Read from the panel's declaration first and the template's second. They
    agree today; the panel is preferred because it is the one a client has
    already used, and an installer offering a choice the panel does not is an
    install that cannot be edited afterwards.
    """
    panel = entry.get("panel") or {}
    if panel.get("kind") == "choice" and panel.get("options"):
        return list(panel["options"])
    return list((entry.get("env") or {}).get("options") or [])


def governed_by(entry: dict) -> tuple[str, list[str]]:
    """The choice field and values that make this field relevant, or ("", []).

    The same `depends` the settings page hides fields on, which is the same
    fact the converge acts on. Asking a client for a Headscale server address
    after they chose the hosted network is asking for a value nothing will read.
    """
    depends = entry.get("depends") or {}
    if not depends:
        return "", []
    return str(depends.get("choice") or ""), list(depends.get("values") or [])
