"""A tagged include_tasks must APPLY its tags to the tasks it includes.

`include_tasks` with `tags:` fires the include when the tag is selected, but
the tasks INSIDE it are filtered out unless `apply:` re-tags them. The result
is a tag-scoped converge that does nothing while reporting ok/changed=0 --
indistinguishable from a role with nothing left to do.

Twice now:

  * playbooks/site.yml's on-box config loader. `--tags postgres` published no
    vault_* facts, every role fell back to the inventory placeholder, and
    fi_s3 caught it as a poisoned postgres password surviving the converge.
  * reconcile/roles/keycloak's realm bootstrap. `--tags keycloak_realm` never ran the
    realm-render tasks, so keycloak-config-cli imported the realm files an
    earlier converge had left. A Settings change already recorded in the
    on-box store could not reach the realm, which bench run
    2026-08-30T19-22-52-155e reported as "expected mfa_enforced=true after
    enforce, got False" beside a realm converge of ok=28 changed=0.

Both were found from the outside, by a scenario, long after the fact. The
shape is checkable directly.

Run: uv run pytest tests/unit/test_tagged_includes_apply_their_tags.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

_ANSIBLE = Path(__file__).resolve().parents[2]
_INCLUDE_KEYS = ("ansible.builtin.include_tasks", "include_tasks")
# `always` needs no apply: it is never filtered out in the first place.
_EXEMPT = {"always"}


def _tasks(node):
    """Yield every task dict, descending into block/rescue/always."""
    if isinstance(node, list):
        for item in node:
            yield from _tasks(item)
        return
    if not isinstance(node, dict):
        return
    yield node
    for key in ("block", "rescue", "always"):
        if key in node:
            yield from _tasks(node[key])


def _offenders() -> list[str]:
    bad: list[str] = []
    roots = [_ANSIBLE / "roles", _ANSIBLE / "playbooks"]
    for root in roots:
        for path in sorted(root.rglob("*.yml")):
            if ".collections" in path.parts:
                continue
            try:
                doc = yaml.safe_load(path.read_text())
            except Exception:
                continue
            for task in _tasks(doc):
                inc = None
                for key in _INCLUDE_KEYS:
                    if key in task:
                        inc = task[key]
                        break
                if inc is None:
                    continue
                tags = [t for t in (task.get("tags") or []) if t not in _EXEMPT]
                if not tags:
                    continue
                applied = []
                if isinstance(inc, dict):
                    applied = (inc.get("apply") or {}).get("tags") or []
                missing = [t for t in tags if t not in applied]
                if missing:
                    rel = path.relative_to(_ANSIBLE)
                    bad.append(f"{rel}: {task.get('name')!r} does not apply {missing}")
    return bad


def test_every_tagged_include_applies_its_tags():
    offenders = _offenders()
    assert not offenders, (
        "these includes carry tags their inner tasks never receive, so a "
        "tag-scoped converge silently does nothing:\n  "
        + "\n  ".join(offenders)
    )
