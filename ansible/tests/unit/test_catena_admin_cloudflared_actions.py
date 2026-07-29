"""Lock down the catena-admin wiring for the Cloudflare tunnel engine.

Covers: the two COMMUNITY cloudflared reserved actions (check + sync), their
env-pin + candidate-token-via-stdin shape, the cloudflare-mode-only gate in
catalog.yml, sshd AcceptEnv for the dispatch env vars, the unconditional host
payload install, and the un-gated ee-install-engines wording.

Run: uv run pytest tests/unit/test_catena_admin_cloudflared_actions.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

_ROLE = (
    Path(__file__).resolve().parents[3]
    / "ansible" / "roles" / "catena-admin"
)
DEFAULTS = _ROLE / "defaults" / "main.yml"
HOST = _ROLE / "tasks" / "host.yml"
CATALOG = _ROLE / "tasks" / "catalog.yml"
DEPLOY = _ROLE / "tasks" / "deploy.yml"


def _defaults() -> dict:
    return yaml.safe_load(DEFAULTS.read_text())


def _by_name(actions: list[dict]) -> dict[str, str]:
    return {a["name"]: a["shell"] for a in actions}


# --- the two Cloudflare tunnel reserved actions (Community) ------------------
def test_cloudflared_reserved_actions_present():
    d = _defaults()
    actions = _by_name(d["catena_admin_cloudflared_reserved_actions"])
    assert set(actions) == {"cloudflared-check", "cloudflared-sync"}


def test_cloudflared_check_reads_candidate_token_on_stdin():
    d = _defaults()
    shell = _by_name(d["catena_admin_cloudflared_reserved_actions"])["cloudflared-check"]
    assert "$CATENA_CF_CANDIDATE_TOKEN" in shell
    assert "check --token-stdin" in shell
    assert "catena-cloudflared-sync" in shell


def test_cloudflared_sync_exports_pins_no_token():
    d = _defaults()
    shell = _by_name(d["catena_admin_cloudflared_reserved_actions"])["cloudflared-sync"]
    assert shell.strip().startswith("env")
    for pin in ("CLOUDFLARED_IMAGE=", "CLOUDFLARED_INGRESS_SERVICE=",
                "CLOUDFLARED_TUNNEL_NAME=", "CATENA_NETWORK=",
                "CLOUDFLARED_STOP_GRACE="):
        assert pin in shell, f"cloudflared-sync missing pin {pin}"
    assert shell.rstrip().endswith("sync")
    # The token is read from the store by the engine, never passed here.
    assert "CATENA_CF_CANDIDATE_TOKEN" not in shell
    assert "cloudflare_api_token" not in shell


def test_cloudflared_actions_are_community_not_ee():
    d = _defaults()
    ee_names = {a["name"] for a in d["catena_admin_ee_reserved_actions"]}
    assert "cloudflared-check" not in ee_names
    assert "cloudflared-sync" not in ee_names


def test_catalog_authorizes_the_cloudflared_actions_unconditionally():
    """They used to be appended only in cloudflare access mode. That mode is
    gone, and the gate was backwards for the deferred-tunnel path anyway: the
    Settings panel is where the token gets entered, so the actions have to be
    authorized on a host that has no tunnel yet."""
    text = CATALOG.read_text()
    assert "catena_admin_cloudflared_reserved_actions" in text
    assert "access_mode" not in text


# --- sshd AcceptEnv for the dispatch env vars -------------------------------
def test_sshd_acceptenv_lists_the_dispatch_env_vars():
    text = HOST.read_text()
    assert "CATENA_CF_CANDIDATE_TOKEN" in text
    assert "CATENA_MULTIDOMAIN_ENABLED" in text
    assert "X_FORWARDED_EMAIL" in text


# --- unconditional host payload install -------------------------------------
def test_ee_install_engines_points_at_payload_installer():
    d = _defaults()
    ee = _by_name(d["catena_admin_ee_reserved_actions"])
    assert "ee-install-engines" in ee
    assert "catena_admin_ee_payload_installer" in ee["ee-install-engines"]


def test_deploy_installs_payload_every_converge():
    text = DEPLOY.read_text()
    # A converge-time install task exists (not just the reserved action).
    assert "install the host engine payload" in text
    assert "catena_admin_ee_payload_installer" in text


def test_defaults_expose_payload_paths():
    d = _defaults()
    assert d["catena_admin_ee_payload_dir"].endswith("/ee-payload")
    assert d["catena_admin_ee_payload_installer"].endswith("install-ee-payload.sh")
    assert d["catena_admin_ee_payload_bin_dir"].endswith("/bin")
