"""The catena-admin wiring for the Cloudflare tunnel engine on the converge
side: the sshd AcceptEnv entry carrying the candidate token, a converge that
declares no Cloudflare action (catena-admin payload/actions.d/20-catena.sh
holds them) and the host payload install in reconcile/roles/payload.

Run: uv run pytest tests/unit/test_catena_admin_cloudflared_actions.py
"""
from __future__ import annotations

import sys

from ansible_tree import ANSIBLE, GROUP_VARS, panel_vars, role_dir

sys.path.insert(0, str(ANSIBLE / "helpers"))

import dispatch_path  # noqa: E402

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
    for name in _by_name(d.get("catena_admin_actions") or []):
        assert not name.startswith("cloudflared-"), (
            f"{name} is declared in catena_admin_actions; it belongs to the image drop-in"
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
    log. AcceptEnv and the sudoers env_keep list are converge-owned permanently
    (reconcile/roles/dispatch_path): a drop-in may use a name that is already on
    them and can never add one."""
    accept = dispatch_path.acceptenv_rule(
        "runner", panel_vars()["catena_admin_dispatch_env_passthrough"])
    assert "CATENA_CF_CANDIDATE_TOKEN" in accept
    assert "X_FORWARDED_EMAIL" in accept


def test_the_passthrough_list_still_carries_the_candidate_token():
    d = panel_vars()
    assert "CATENA_CF_CANDIDATE_TOKEN" in d["catena_admin_dispatch_env_passthrough"], (
        "the image's cloudflared-check arm reads this variable and cannot "
        "authorise it for itself"
    )


# --- unconditional host payload install -------------------------------------
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
