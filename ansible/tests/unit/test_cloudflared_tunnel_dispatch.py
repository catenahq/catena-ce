"""Lock down the slimmed roles/cloudflare_tunnel.

The tunnel/DNS/ingress/cloudflared-service converge moved into the Go engine
catena-cloudflared-sync (host payload). The role now only dispatches that
engine's `sync` when the on-box store holds a Cloudflare token, and defers
otherwise. These tests pin that shape and prove the old converge-time CF-API /
swarm-service tasks are gone.

Run: uv run pytest tests/unit/test_cloudflared_tunnel_dispatch.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

_ROLE = (
    Path(__file__).resolve().parents[3]
    / "ansible" / "roles" / "cloudflare_tunnel"
)
TASKS = _ROLE / "tasks" / "main.yml"
DEFAULTS = _ROLE / "defaults" / "main.yml"


def _tasks() -> list[dict]:
    return yaml.safe_load(TASKS.read_text())


def _defaults() -> dict:
    return yaml.safe_load(DEFAULTS.read_text())


def _find(name_fragment: str) -> dict:
    for task in _tasks():
        if name_fragment in (task.get("name") or ""):
            return task
    raise AssertionError(f"task not found: {name_fragment!r}")


# --- the ported CF-API + swarm-service tasks are GONE -----------------------
def test_no_cloudflare_api_tasks_remain():
    """No ansible.builtin.uri calls -- the CF API find-or-create / DNS / ingress
    all live in the Go engine now."""
    for task in _tasks():
        assert "ansible.builtin.uri" not in task, (
            f"CF API task still in the role: {task.get('name')!r} -- that logic "
            "belongs to catena-cloudflared-sync now"
        )


def test_no_cloudflared_swarm_service_management():
    names = " ".join((t.get("name") or "") for t in _tasks()).lower()
    for gone in ("swarm service", "tunnel token", "wildcard dns",
                 "network reconcile", "network attachments"):
        assert gone not in names, f"{gone!r} task should be gone from the role"


# --- token-gated dispatch ---------------------------------------------------
def test_reads_token_presence_from_store_fact():
    task = _find("is a Cloudflare token present")
    fact = task["ansible.builtin.set_fact"]
    assert "_cf_token_present" in fact
    assert "vault_cloudflare_api_token" in fact["_cf_token_present"]


def test_deferred_debug_when_no_token():
    task = _find("tunnel deferred")
    when = task["when"]
    cond = " ".join(when) if isinstance(when, list) else when
    assert "not (_cf_token_present | bool)" in cond
    assert "Settings" in task["ansible.builtin.debug"]["msg"]


def test_dispatches_sync_engine_with_pins():
    task = _find("converge the tunnel via catena-cloudflared-sync")
    argv = task["ansible.builtin.command"]["argv"]
    assert argv[-1] == "sync"
    assert "cloudflared_sync_bin" in argv[0]
    env = task["environment"]
    for pin in ("CLOUDFLARED_IMAGE", "CLOUDFLARED_INGRESS_SERVICE",
                "CLOUDFLARED_TUNNEL_NAME", "CATENA_NETWORK",
                "CLOUDFLARED_STOP_GRACE", "CATENA_MULTIDOMAIN_ENABLED"):
        assert pin in env, f"missing engine pin {pin} in the sync dispatch env"
    # The token is NEVER passed to the engine (it reads the store).
    assert not any("vault_cloudflare_api_token" in str(v) for v in env.values())


def test_sync_gated_on_token_and_binary():
    task = _find("converge the tunnel via catena-cloudflared-sync")
    cond = " ".join(task["when"])
    assert "_cf_token_present | bool" in cond
    assert "_cf_sync_bin.stat.exists" in cond


def test_sync_reports_unchanged_for_idempotency_gate():
    task = _find("converge the tunnel via catena-cloudflared-sync")
    assert task["changed_when"] is False


def test_multidomain_flag_gated_on_ee_license_var():
    task = _find("converge the tunnel via catena-cloudflared-sync")
    md = task["environment"]["CATENA_MULTIDOMAIN_ENABLED"]
    assert "catena_ee_multidomain_enabled" in md


def test_le_warning_preserved():
    task = _find("tunnel converged")
    assert "DO NOT enable Let's Encrypt" in task["ansible.builtin.debug"]["msg"]


# --- defaults carry the non-secret engine pins ------------------------------
def test_defaults_expose_engine_bin_and_pins():
    d = _defaults()
    assert d["cloudflared_sync_bin"] == "/usr/local/bin/catena-cloudflared-sync"
    assert d["cloudflared_stop_grace"] == "40s"
    assert d["cloudflared_probe_attempts"] == 60
    assert d["cloudflared_reprobe_attempts"] == 30
    assert d["cloudflared_probe_delay_seconds"] == 10
