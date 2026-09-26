"""A whole-server restore converges without moving the public edge.

catena-admin payload/cmd/catena-recovery runs reconcile.yml with
--skip-tags cloudflare,mailserver_dns,coturn_dns (edgeTags there): pointing the
domain at a restored host is its own step. ansible-playbook accepts a skip tag
nothing carries, so a DNS writer added under another tag, or a tag renamed,
would repoint public records from inside a restore without any error.

A DNS writer is a file in a role reconcile.yml runs that calls the Cloudflare
API (cloudflare_api_base). Each one is pinned here with the tag that skips it.

Run: uv run pytest tests/unit/test_restore_converge_skips_every_dns_writer.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
RESTORE_SKIP_TAGS = {"cloudflare", "mailserver_dns", "coturn_dns"}

# writer -> (file that pulls it in, the include's file value, its skip tag)
WRITERS = {
    "reconcile/roles/cloudflare_tunnel": ("playbooks/reconcile.yml", None, "cloudflare"),
    "reconcile/roles/infrastructure/tasks/mailserver_dns.yml": (
        "reconcile/roles/infrastructure/tasks/main.yml", "mailserver_dns.yml", "mailserver_dns"),
    "reconcile/roles/coturn/tasks/dns.yml": (
        "reconcile/roles/coturn/tasks/deploy.yml", "dns.yml", "coturn_dns"),
}


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


def _reconcile_roles() -> dict[str, set[str]]:
    play = yaml.safe_load((ANSIBLE / "playbooks" / "reconcile.yml").read_text())[0]
    return {r["role"]: set(r.get("tags", [])) for r in play["roles"] if isinstance(r, dict)}


def test_every_reconcile_file_calling_cloudflare_is_a_known_dns_writer():
    callers = {
        str(p.relative_to(ANSIBLE))
        for role in _reconcile_roles()
        for p in (ANSIBLE / "reconcile" / "roles" / role).rglob("*.yml")
        if "/defaults/" not in str(p) and "cloudflare_api_base" in p.read_text()
    }
    assert "reconcile/roles/coturn/tasks/dns.yml" in callers, "the scan sees no writer"
    unknown = {c for c in callers
               if not any(c == w or c.startswith(w + "/") for w in WRITERS)}
    assert not unknown, (
        "these reconcile files call the Cloudflare API and no restore skip tag "
        "covers them; tag the include and add the tag to edgeTags in catena-admin "
        f"payload/cmd/catena-recovery/main.go and to this test: {sorted(unknown)}")


def test_the_tunnel_role_carries_the_cloudflare_tag():
    assert "cloudflare" in _reconcile_roles()["cloudflare_tunnel"]


def test_each_dns_task_file_is_included_under_its_skip_tag():
    for writer, (includer, target, tag) in WRITERS.items():
        if target is None:
            continue
        assert tag in RESTORE_SKIP_TAGS
        tasks = yaml.safe_load((ANSIBLE / includer).read_text())
        sites = [t for t in _walk(tasks) if _include_target(t) == target]
        assert sites, f"{includer} no longer includes {target} ({writer})"
        for site in sites:
            assert tag in (site.get("tags") or []), (
                f"{includer} includes {target} without the {tag} tag; a restore "
                "converge would run it")
