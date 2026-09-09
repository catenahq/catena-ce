"""Lock down the catena-admin wiring for the Cloudflare tunnel engine.

The two Cloudflare dispatch actions are NOT here. They live in the image, as
catena-admin payload/actions.d/20-catena.sh, because neither body interpolates
a fact about the host: the engine reads the token, the zones and the tunnel-name
prefix out of the on-box store and names the tunnel after the box's own
hostname. What this repo still owns for them is the sshd AcceptEnv entry that
carries the candidate token -- a drop-in may read that name and can never add to
it -- so that is what is asserted here, together with the converge's silence.

Also covers the hand-off of the host payload install to roles/payload and the
un-gated ee-install-engines wording.

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
# The bootstrap-side half of the same panel: the runner account, the
# sudoers drop-in, the forced command. Phase 1b made it a role of its
# own so the boundary it was declared on could actually be held.
_HOST_ROLE = _ROLE.parent / "catena_admin_host"
_HOST_DEFAULTS = _HOST_ROLE / "defaults" / "main.yml"
# And the values both halves read, which belong to neither role.
_GROUP_VARS = (
    _ROLE.parents[1] / "playbooks" / "group_vars" / "all" / "main.yml")
HOST = _HOST_ROLE / "tasks" / "main.yml"
CATALOG = _ROLE / "tasks" / "catalog.yml"
DEPLOY = _ROLE / "tasks" / "deploy.yml"
PAYLOAD_ROLE = _ROLE.parent / "payload"


def _defaults() -> dict:
    """Every variable the panel's converge reads, from all three places it now
    lives.

    Phase 1b split the role: the trust path is roles/catena_admin_host, the
    container is roles/catena-admin, and the values BOTH halves need are in
    group_vars because a role default is only dependable once that role has run.
    Merged here so an assertion is about the panel's configuration rather than
    about which file happens to hold a line today.
    """
    merged: dict = {}
    for path in (_GROUP_VARS, DEFAULTS, _HOST_DEFAULTS):
        merged.update(yaml.safe_load(path.read_text()) or {})
    return merged


def _by_name(actions: list[dict]) -> dict[str, str]:
    return {a["name"]: a["shell"] for a in actions}


# --- the converge does not carry the Cloudflare actions ----------------------
def test_the_converge_declares_no_cloudflared_action():
    """The dispatcher is base-first, so a name in the converge's table wins
    outright and the drop-in where that command is maintained is never reached
    -- silently, with both files passing every assertion about themselves.

    An empty list left behind is a place for one to come back to, so there is no
    list."""
    d = _defaults()
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
                 Path(__file__).resolve().parents[2]
                 / "playbooks" / "group_vars" / "all" / "main.yml"):
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
    d = _defaults()
    assert "CATENA_CF_CANDIDATE_TOKEN" in d["catena_admin_dispatch_env_passthrough"], (
        "the image's cloudflared-check arm reads this variable and cannot "
        "authorise it for itself"
    )


# --- unconditional host payload install -------------------------------------
def test_ee_install_engines_dispatches_the_host_verb():
    d = _defaults()
    ee = _by_name(d["catena_admin_ee_reserved_actions"])
    assert "ee-install-engines" in ee
    assert "catena_admin_stack_update_bin" in ee["ee-install-engines"]
    assert "install-payload" in ee["ee-install-engines"]


def test_no_root_dispatch_runs_out_of_the_container_writable_mirror():
    """{{ catena_admin_ee_payload_dir }} is chowned to the container's uid so
    the shell's startup mirror can write it. A root dispatch pointed at a
    script inside it makes the panel -- built with no root and no docker socket
    for exactly this reason -- the thing choosing what root executes.

    The same property on the drop-in side is asserted by catena-admin
    payload/actions.d/actions_test.go, against the files that carry those
    commands now."""
    d = _defaults()
    mirror = d["catena_admin_ee_payload_dir"]
    for name, shell in _by_name(d["catena_admin_ee_reserved_actions"]).items():
        assert "catena_admin_ee_payload_installer" not in shell, (
            f"{name} runs the installer out of the mirror")
        assert mirror not in shell, f"{name} reads root input from {mirror}"


def test_deploy_no_longer_owns_the_payload_install():
    """The install moved to roles/payload, four roles ahead of this one.

    Leaving a second installer here would reinstall the engines from the
    container's mirrored copy after roles/payload already installed them from
    the image -- two writers racing over /usr/local/bin, and the loser is
    whichever image is staler.
    """
    text = DEPLOY.read_text()
    assert "install the host engine payload" not in text
    assert "wait for the host payload to sync" not in text


def test_payload_role_owns_the_install():
    text = (PAYLOAD_ROLE / "tasks" / "main.yml").read_text()
    assert "install-ee-payload.sh" in text
    assert "catena_payload_marker" in text


def test_defaults_expose_payload_paths():
    d = _defaults()
    assert d["catena_admin_ee_payload_dir"].endswith("/ee-payload")
    assert d["catena_admin_ee_payload_installer"].endswith("install-ee-payload.sh")
