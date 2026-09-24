"""oauth2-proxy validation skips whenever the tunnel is deferred.

The per-app probes wait on https://auth.<zone>, which only answers once
reconcile/roles/cloudflare_tunnel has brought the edge up. That role defers
for two reasons -- no token in the store, or its engine not on the host yet --
and pins the second as _cf_sync_present. Gating the validation on the token
alone probed an edge the same converge had just declined to build.

Run: uv run pytest tests/unit/test_oauth2_proxy_validate_follows_the_tunnel.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

VALIDATE = (
    Path(__file__).resolve().parents[2]
    / "reconcile" / "roles" / "oauth2_proxy" / "tasks" / "validate.yml"
)


def _task(fragment: str) -> dict:
    for task in yaml.safe_load(VALIDATE.read_text()):
        if fragment in (task.get("name") or ""):
            return task
    raise AssertionError(f"task not found: {fragment!r}")


def _when(task: dict) -> str:
    when = task.get("when")
    return " and ".join(when) if isinstance(when, list) else str(when)


def test_the_skip_names_both_deferral_reasons():
    when = _when(_task("tunnel deferred"))
    assert "cloudflare_api_token" in when
    assert "_cf_sync_present" in when


def test_the_probes_run_only_when_the_edge_can_be_up():
    when = _when(_task("per-app container probes"))
    assert "cloudflare_api_token" in when
    assert "_cf_sync_present" in when


def test_a_play_without_the_tunnel_role_still_validates():
    """Undefined must read as present, or a play that never ran the tunnel role
    would skip validation on a host whose edge is up."""
    assert "_cf_sync_present | default(true)" in _when(
        _task("per-app container probes"))


INFRA_MAIN = (
    Path(__file__).resolve().parents[2]
    / "reconcile" / "roles" / "infrastructure" / "tasks" / "main.yml"
)


def test_the_infrastructure_edge_gate_also_follows_the_tunnel():
    """Same deferral, one role later: the gated-services public-URL probe and
    the mailserver chain both need the edge, and gating them on the token
    alone failed the no-tailnet install at a 530 (run
    2026-09-24T03-24-43-1c7c)."""
    text = INFRA_MAIN.read_text()
    gate = next(t for t in yaml.safe_load(text)
                if t.get("name") == "Resolve the tunnel-deferred gate")
    expr = gate["ansible.builtin.set_fact"]["_infra_edge_up"]
    assert "cloudflare_api_token" in expr
    assert "_cf_sync_present | default(true)" in expr
    assert "_infra_cf_token_present" not in text, (
        "a gate still reads the token-only fact")


INFRA_VALIDATE = INFRA_MAIN.parent / "validate.yml"
PLAYBOOK_VALIDATE = Path(__file__).resolve().parents[2] / "playbooks" / "validate.yml"


def test_the_standalone_validate_asks_whether_the_engine_is_here():
    """Run 2026-09-24T15-24-09-c565: the converge now deferred cleanly, and the
    install's validate then asserted cloudflared 1/1 because a token existed."""
    tasks = yaml.safe_load(INFRA_VALIDATE.read_text())
    edge = next(t for t in tasks
                if "resolve whether the edge should be up" in (t.get("name") or ""))
    assert "catena_payload_missing" in edge["ansible.builtin.set_fact"]["_infra_edge_up"]
    for t in tasks:
        if "cloudflared" in (t.get("name") or ""):
            assert "_infra_edge_up" in str(t.get("when")), t["name"]


def test_the_via_tunnel_probes_follow_the_edge():
    text = PLAYBOOK_VALIDATE.read_text()
    assert "_infra_edge_up" in text
    for name in ("Tunnel probes deferred", "Probe Keycloak (auth.<zone>)",
                 "Build gated-host probe set"):
        block = text[text.index(name):]
        when = block[:block.index("\n    - name:")] if "\n    - name:" in block else block
        assert "_v2_edge_up" in when, name
