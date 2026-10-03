"""A panel field's `default` is the value the converge falls back to.

The settings page shows `panel.default` in a field while the store holds
nothing, and the converge resolves the same unset value from a literal in its
own defaults (`cfg_x | default('literal', true)`). The two have to agree, or
the page shows one name while the server answers on another.

Run: uv run pytest tests/unit/test_panel_defaults_match_the_converge.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ANSIBLE_DIR = Path(__file__).resolve().parents[2]
if str(ANSIBLE_DIR) not in sys.path:
    sys.path.insert(0, str(ANSIBLE_DIR))

from helpers import render_knobs  # noqa: E402


def _fallbacks(var: str) -> set[str]:
    """Every literal the Ansible tree falls back to for `var`."""
    pattern = re.compile(re.escape(var) + r"\s*\|\s*default\('([^']*)'")
    found: set[str] = set()
    for path in ANSIBLE_DIR.rglob("*.yml"):
        if {".collections", "tests"} & set(path.parts):
            continue
        found.update(pattern.findall(path.read_text(encoding="utf-8")))
    return found


def test_every_panel_default_is_the_converges_fallback():
    doc = render_knobs.load()
    checked = 0
    for knob in doc["config"]:
        default = (knob.get("panel") or {}).get("default")
        if default is None:
            continue
        checked += 1
        assert "var" in knob, f"{knob['key']}: a default with no fact to fall back on"
        assert _fallbacks(knob["var"]) == {default}, (
            f"{knob['key']}: the panel shows {default!r}, the converge falls "
            f"back to {sorted(_fallbacks(knob['var']))}")
    assert checked, "no panel field declares a default; this check is vacuous"
