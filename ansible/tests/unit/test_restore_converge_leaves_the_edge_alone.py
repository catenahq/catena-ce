"""A whole-server restore converges without touching the public edge.

catena-admin payload/cmd/catena-recovery runs reconcile.yml with
--skip-tags edge. The public edge (the tunnel and the mail and TURN A records)
moves at a migration's CUTOVER or a later converge, and until then it may point
at another host: a migration target's auth.<zone> answers Cloudflare 530/1033
while the source is stopped, so a check through it fails a restore that is
working.

So `edge` must reach everything that MOVES the edge (a Cloudflare API caller,
cloudflare_api_base) and everything that ASSERTS it (an HTTPS probe of a
public hostname). ansible-playbook accepts a skip tag nothing carries, so a new
caller or probe outside `edge` would run inside a restore without any error.

Run: uv run pytest tests/unit/test_restore_converge_leaves_the_edge_alone.py
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
EDGE = "edge"
# The uri module's `url:` key, not a key that merely ends in url (a seed's
# oidc_auth_url names an address, it requests none).
_PUBLIC_PROBE = re.compile(r"\burl:\s*[\"']?https://")


def _reconcile_roles() -> dict[str, set[str]]:
    play = yaml.safe_load((ANSIBLE / "playbooks" / "reconcile.yml").read_text())[0]
    return {r["role"]: set(r.get("tags", [])) for r in play["roles"] if isinstance(r, dict)}


def _walk(tasks):
    for t in tasks or []:
        if not isinstance(t, dict):
            continue
        yield t
        for key in ("block", "rescue", "always"):
            yield from _walk(t.get(key))


def _include_target(task):
    for key in ("ansible.builtin.include_tasks", "ansible.builtin.import_tasks",
                "include_tasks", "import_tasks"):
        v = task.get(key)
        if isinstance(v, str):
            return v
        if isinstance(v, dict):
            return v.get("file")
    return None


def _tags(task) -> set[str]:
    tags = task.get("tags") or []
    return {tags} if isinstance(tags, str) else set(tags)


def _files_outside_edge(role: str) -> set[Path]:
    """Task files of `role` a restore converge still runs: reachable from
    tasks/main.yml through includes none of which carries `edge`."""
    tasks_dir = ANSIBLE / "reconcile" / "roles" / role / "tasks"
    reached: set[Path] = set()
    queue = [tasks_dir / "main.yml"]
    while queue:
        p = queue.pop()
        if p in reached or not p.is_file():
            continue
        reached.add(p)
        for t in _walk(yaml.safe_load(p.read_text()) or []):
            target = _include_target(t)
            if target and "{{" not in target and EDGE not in _tags(t):
                queue.append(tasks_dir / target)
    return reached


def _outside_edge() -> dict[Path, str]:
    out: dict[Path, str] = {}
    for role, tags in _reconcile_roles().items():
        if EDGE in tags:
            continue
        for p in _files_outside_edge(role):
            out[p] = role
    return out


def test_the_tunnel_role_carries_the_edge_tag():
    assert EDGE in _reconcile_roles()["cloudflare_tunnel"]


def test_nothing_a_restore_runs_calls_the_cloudflare_api():
    callers = [p for p in _outside_edge() if "cloudflare_api_base" in p.read_text()]
    assert not callers, (
        "a restore converge (--skip-tags edge) would run these Cloudflare API "
        "callers; include them under the `edge` tag: "
        f"{sorted(str(p.relative_to(ANSIBLE)) for p in callers)}")


def test_nothing_a_restore_runs_probes_a_public_hostname():
    probes = [p for p in _outside_edge() if _PUBLIC_PROBE.search(p.read_text())]
    assert not probes, (
        "a restore converge (--skip-tags edge) would probe the public edge from "
        "these files; include them under the `edge` tag: "
        f"{sorted(str(p.relative_to(ANSIBLE)) for p in probes)}")


def test_the_scan_sees_the_edge_tagged_files():
    """A walk that stopped following includes would pass anything."""
    outside = {str(p.relative_to(ANSIBLE)) for p in _outside_edge()}
    assert "reconcile/roles/infrastructure/tasks/main.yml" in outside
    assert "reconcile/roles/oauth2_proxy/tasks/deploy.yml" in outside
    for edge_file in ("reconcile/roles/infrastructure/tasks/mailserver_dns.yml",
                      "reconcile/roles/infrastructure/tasks/verify_gated_services.yml",
                      "reconcile/roles/coturn/tasks/dns.yml",
                      "reconcile/roles/oauth2_proxy/tasks/validate.yml"):
        assert (ANSIBLE / edge_file).is_file(), edge_file
        assert edge_file not in outside, f"{edge_file} is reachable outside `edge`"
