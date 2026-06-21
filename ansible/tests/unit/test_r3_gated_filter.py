"""Lock down the R3 gated-host derivation in playbooks/validate.yml.

R3 enumerates every Dokploy compose, fetches its composeFile, and
classifies it as gated unless the composeFile carries the
`vps.auth.mode=public` label. The classification is implemented as a
Jinja `rejectattr(... 'search', '<pattern>')` test -- i.e. a regex
match against the raw composeFile body.

This test guards two regressions that previously let every public-
labelled template (and OliveTin) leak into the gated probe set,
producing 502 / 200 false positives:

  1. The pattern reaches Jinja exactly as the YAML scalar literal --
     YAML single-quoted scalars do NOT process backslash escapes, so
     `'vps\\.auth\\.mode=public'` becomes the regex `vps\\.auth\\.mode=public`,
     which requires a literal backslash in the input and never matches
     real composeFile labels. The pattern must use single backslashes.
  2. The keycloak host (auth.<zone>) carries no `vps.auth.mode` label
     and would otherwise survive the rejectattr filter; the build-set
     step must drop it explicitly.

Run: `uv run pytest tests/unit/test_r3_gated_filter.py`
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
VALIDATE_YML = REPO_ROOT / "ansible" / "playbooks" / "validate.yml"


def _load_validate_tasks() -> list[dict]:
    text = VALIDATE_YML.read_text()
    plays = yaml.safe_load(text)
    tasks: list[dict] = []
    for play in plays:
        for key in ("tasks", "pre_tasks", "post_tasks"):
            for task in play.get(key) or []:
                tasks.append(task)
                if "block" in task:
                    tasks.extend(task["block"] or [])
    return tasks


def _find_task(name: str) -> dict:
    for task in _load_validate_tasks():
        if task.get("name") == name:
            return task
    raise AssertionError(f"task not found: {name!r}")


def _extract_pattern(rejectattr_call: str) -> str:
    """Pull the regex literal out of a `rejectattr(...)` filter call."""
    m = re.search(
        r"rejectattr\(\s*'json\.composeFile'\s*,\s*'search'\s*,\s*'([^']*)'\s*\)",
        rejectattr_call,
    )
    assert m, f"rejectattr literal not found in: {rejectattr_call!r}"
    return m.group(1)


def test_public_label_pattern_matches_real_compose_label():
    """The regex literal must match the canonical label syntax."""
    task = _find_task("Derive gated hosts (no vps.auth.mode=public label)")
    expr = task["ansible.builtin.set_fact"]["_r3_dyn_gated_hosts"]
    pattern = _extract_pattern(expr)

    # Real compose body contains lines like:  - "vps.auth.mode=public"
    label_line = '      - "vps.auth.mode=public"'
    private_line = '      - "vps.auth.mode=private"'

    assert re.search(pattern, label_line), (
        f"pattern {pattern!r} did NOT match a real `vps.auth.mode=public` "
        "label line. The likely cause is double-backslash over-escaping "
        "(YAML single-quoted scalars don't process \\\\ as \\)."
    )
    assert not re.search(pattern, private_line), (
        f"pattern {pattern!r} unexpectedly matched a `private` label line."
    )


def test_public_label_pattern_uses_escaped_dots():
    """Pattern must escape literal dots -- bare `.` would over-match
    (e.g. `vpsxauthxmode=public` would also match)."""
    task = _find_task("Derive gated hosts (no vps.auth.mode=public label)")
    expr = task["ansible.builtin.set_fact"]["_r3_dyn_gated_hosts"]
    pattern = _extract_pattern(expr)

    assert re.search(r"vps\\\.auth\\\.mode=public", pattern), (
        f"pattern {pattern!r} should use \\. between segments to anchor "
        "to the literal dot."
    )


def test_build_set_drops_keycloak_host():
    """Keycloak's compose has no `vps.auth.mode` label (it IS the IdP),
    so it survives the rejectattr filter. The build-set step must drop
    it explicitly so R3 doesn't probe `auth.<zone>/oauth2/start`."""
    task = _find_task("Build gated-host probe set")
    expr = task["ansible.builtin.set_fact"]["_gated_hosts"]

    assert "keycloak_hostname" in expr, (
        "build-set step is missing an explicit drop for keycloak_hostname; "
        "auth.<zone> would otherwise be probed as a gated app."
    )
    assert re.search(r"reject\(\s*'equalto'\s*,\s*keycloak_hostname", expr), (
        "expected `reject('equalto', keycloak_hostname ...)` in the "
        f"build-set expression: {expr!r}"
    )
