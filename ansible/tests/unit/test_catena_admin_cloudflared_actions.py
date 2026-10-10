"""The catena-admin wiring for the Cloudflare tunnel engine on the converge
side: the sshd AcceptEnv entry carrying the candidate token, a converge that
declares no Cloudflare action (catena-admin payload/actions.d/20-catena.sh
holds them), the host payload install in reconcile/roles/payload, and the
ee-install-engines dispatch.

Run: uv run pytest tests/unit/test_catena_admin_cloudflared_actions.py
"""
from __future__ import annotations

import os
import subprocess

import jinja2

from ansible_tree import GROUP_VARS, PANEL_HOST_TASKS as HOST, panel_vars, role_dir

_ROLE = role_dir("catena-admin")
DEFAULTS = _ROLE / "defaults" / "main.yml"
CATALOG = _ROLE / "tasks" / "catalog.yml"
DEPLOY = _ROLE / "tasks" / "deploy.yml"
PAYLOAD_ROLE = _ROLE.parent / "payload"


def _by_name(actions: list[dict]) -> dict[str, str]:
    return {a["name"]: a["shell"] for a in actions}


# --- the converge does not carry the Cloudflare actions ----------------------
def test_the_converge_declares_no_cloudflared_action():
    """The dispatcher is base-first, so a name in the converge's table wins
    outright and the drop-in where that command is maintained is never reached
    -- silently, with both files passing every assertion about themselves.

    An empty list left behind is a place for one to come back to, so there is no
    list."""
    d = panel_vars()
    assert "catena_admin_cloudflared_reserved_actions" not in d, (
        "the cloudflared reserved list is back. Base-first precedence means "
        "anything in it shadows catena-admin payload/actions.d/20-catena.sh, "
        "where these two are maintained"
    )
    for group in ("catena_admin_ee_reserved_actions",
                  "catena_admin_actions"):
        for name in _by_name(d.get(group) or []):
            assert not name.startswith("cloudflared-"), (
                f"{name} is declared in {group}; it belongs to the image drop-in"
            )


def test_the_catalog_merges_no_cloudflared_term():
    text = CATALOG.read_text()
    assert "cloudflared" not in text, (
        "catalog.yml names cloudflared again, which puts the actions back in "
        "the converge-rendered dispatch table"
    )


def test_the_converge_hands_the_engine_no_tunnel_name():
    """The name is the whole reason those actions could not ship in the image.

    A tunnel named from inventory_hostname can only be rendered by a converge,
    so an action body carrying one has to be rendered by a converge too. The
    engine composes the name from the box's hostname and the store's prefix, and
    both dispatch paths have to agree -- an engine that gets the name from its
    caller on one path and composes it on the other names two different tunnels.
    """
    tunnel_role = _ROLE.parent / "cloudflare_tunnel"
    for path in (tunnel_role / "tasks" / "main.yml",
                 tunnel_role / "defaults" / "main.yml",
                 DEFAULTS,
                 GROUP_VARS):
        text = path.read_text()
        assert "CLOUDFLARED_TUNNEL_NAME:" not in text, (
            f"{path.name} exports CLOUDFLARED_TUNNEL_NAME to the engine")
        assert "CLOUDFLARED_TUNNEL_NAME=" not in text, (
            f"{path.name} exports CLOUDFLARED_TUNNEL_NAME to the engine")


# --- sshd AcceptEnv for the dispatch env vars -------------------------------
def test_sshd_acceptenv_lists_the_dispatch_env_vars():
    """The one thing the image drop-in cannot do for itself.

    cloudflared-check reads the candidate token out of the SSH environment
    rather than argv, so it never reaches $SSH_ORIGINAL_COMMAND or any process
    log. AcceptEnv and the sudoers env_keep list are converge-owned permanently:
    a drop-in may use a name that is already on them and can never add one."""
    text = HOST.read_text()
    assert "CATENA_CF_CANDIDATE_TOKEN" in text
    assert "X_FORWARDED_EMAIL" in text


def test_the_passthrough_list_still_carries_the_candidate_token():
    d = panel_vars()
    assert "CATENA_CF_CANDIDATE_TOKEN" in d["catena_admin_dispatch_env_passthrough"], (
        "the image's cloudflared-check arm reads this variable and cannot "
        "authorise it for itself"
    )


# --- unconditional host payload install -------------------------------------
def _press_ee_install_engines(tmp_path, systemd_run: str):
    """Run the rendered ee-install-engines arm under the runner's strict mode,
    with a systemd-run that records its argv and then runs `systemd_run`."""
    d = panel_vars()
    shell = jinja2.Template(
        _by_name(d["catena_admin_ee_reserved_actions"])["ee-install-engines"]
    ).render(catena_admin_stack_update_bin=d["catena_admin_stack_update_bin"])
    bindir = tmp_path / "bin"
    bindir.mkdir()
    argv_file = tmp_path / "argv"
    stub = bindir / "systemd-run"
    stub.write_text(f'#!/bin/bash\nprintf "%s\\n" "$@" > "{argv_file}"\n{systemd_run}\n')
    stub.chmod(0o755)
    r = subprocess.run(
        ["bash", "-c", "set -euo pipefail\n" + shell],
        capture_output=True, text=True, timeout=30,
        env=dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}"),
    )
    argv = argv_file.read_text().splitlines() if argv_file.exists() else []
    return r, argv


def test_ee_install_engines_runs_the_verb_detached_under_the_host_lock(tmp_path):
    """The self-update and the host's own converge empty and refill the same
    stage directory and rotate the payload manifest under /run/catena.lock.
    flock sits inside the unit, around the verb: around systemd-run it would
    let go of the lock as soon as the unit started."""
    r, argv = _press_ee_install_engines(tmp_path, "exit 0")
    assert r.returncode == 0, r.stderr
    assert argv == [
        "--unit", "catena-install-payload", "--collect",
        "/usr/bin/flock", "-w", "3600", "/run/catena.lock",
        panel_vars()["catena_admin_stack_update_bin"], "install-payload",
    ]
    assert "started in the background" in r.stdout


def test_ee_install_engines_reports_no_start_when_systemd_run_refuses(tmp_path):
    """A second press while the first unit runs is refused by systemd-run, the
    unit name being in use. The button then fails rather than reporting an
    install that never started."""
    r, _ = _press_ee_install_engines(tmp_path, 'echo "unit exists" >&2; exit 1')
    assert r.returncode != 0
    assert "started" not in r.stdout


def test_deploy_does_not_own_the_payload_install():
    """reconcile/roles/payload installs the engines, earlier in the converge,
    out of the image. A second installer here would be two writers racing over
    /usr/local/bin, and the loser is whichever image is staler.
    """
    text = DEPLOY.read_text()
    assert "install the host engine payload" not in text
    assert "wait for the host payload to sync" not in text


def test_payload_role_owns_the_install():
    text = (PAYLOAD_ROLE / "tasks" / "main.yml").read_text()
    assert "install-ee-payload.sh" in text
    assert "catena_payload_marker" in text
