"""A service its sign-in gate serves gets no Traefik route of its own.

swarm_stack.yml writes a default-priority route for every caller that names a
svc_domain_host. For a gated host that route only waits for the gate's
priority-100 route to go missing, and then serves the app ungated. Every route
it does write targets its own service's alias: `app` is the service name every
stack with an `app` service carries.

Run: uv run pytest tests/unit/test_gated_services_have_no_default_route.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
ROLES = ANSIBLE / "reconcile" / "roles"


def _tasks(node):
    for task in node or []:
        if isinstance(task, dict):
            yield task
            for key in ("block", "rescue", "always"):
                yield from _tasks(task.get(key))


def _stack_deploys() -> list[tuple[str, dict]]:
    out = []
    for path in sorted(ROLES.glob("*/tasks/*.yml")):
        for task in _tasks(yaml.safe_load(path.read_text())):
            if str(task.get("ansible.builtin.include_tasks") or "").endswith("swarm_stack.yml"):
                out.append((f"{path.relative_to(ROLES)}: {task.get('name')}",
                            task.get("vars") or {}))
    return out


def test_every_route_targets_its_own_service():
    routed = [(where, v) for where, v in _stack_deploys() if v.get("svc_domain_host")]
    assert routed, "no swarm_stack.yml caller writes a route; the scan found nothing"
    for where, v in routed:
        assert v.get("svc_domain_service") not in (None, "", "app"), (
            f"{where} routes to {v.get('svc_domain_service')!r}, an alias other "
            f"stacks carry")


def test_no_gated_host_gets_a_default_route():
    gated = {a["host_var"] for a in yaml.safe_load(
        (ROLES / "oauth2_proxy" / "defaults" / "main.yml").read_text())["oauth2_proxy_apps"]}
    for where, v in _stack_deploys():
        host = str(v.get("svc_domain_host") or "")
        assert not any(var in host for var in gated), (
            f"{where} writes a default route for a host its gate serves")


def test_gatus_healthchecks_and_the_hub_write_none():
    deploys = dict(_stack_deploys())
    for where in ("infrastructure/tasks/gatus.yml: Gatus: deploy as a swarm stack",
                  "infrastructure/tasks/healthchecks.yml: Healthchecks: deploy as a swarm stack",
                  "infrastructure/tasks/beszel.yml: Beszel: deploy hub as a swarm stack"):
        assert deploys[where]["svc_domain_host"] == ""
