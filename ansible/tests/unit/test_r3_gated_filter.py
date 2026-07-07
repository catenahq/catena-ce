"""Lock down the gated-host probe set in playbooks/validate.yml.

The dynamic control-plane compose enumeration (project.all + compose.one +
composeFile `vps.auth.mode=public` classification) was removed in the
Portainer migration: client-app gating is now label-based via
dashboard-sync (route_synth / labels_schema), verified by
verify_gated_services.yml, not re-walked in validate. This test guards
the one remaining invariant in the static build-set step.

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


def test_build_set_drops_keycloak_host():
    """Keycloak's host (auth.<zone>) is the IdP, not an oauth2-proxy-gated
    client. The build-set step must drop it explicitly so validate doesn't
    probe `auth.<zone>/oauth2/start` as a gated app."""
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
