"""The env channel the backup-endpoint (S3) candidate check travels through.

A candidate S3 repository plus access/secret key is validated BEFORE the
Settings panel persists it. The three values ride the SSH Runner's env map,
never $PAYLOAD/argv, so they never land in $SSH_ORIGINAL_COMMAND or any host
process argv -- and a name that disagrees on any hop silently breaks the whole
feature: the action sees an empty candidate and the panel is told a valid
endpoint is invalid.

The ACTION moved into the panel image's dispatch drop-in with the other
Community ones, and what it has to look like is asserted there (catena-admin
payload/actions.d/catena_actions_test.go). What stays here is the part that is
about this host rather than about the command: sshd's AcceptEnv and the sudoers
env_keep, which are converge-owned permanently. The image can own the dispatch
table; the set of inputs root will accept is not a decision it gets to make.

Run: uv run pytest tests/unit/test_catena_admin_backup_endpoint_actions.py
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

CANDIDATE_ENV_VARS = (
    "CATENA_BACKUP_CANDIDATE_REPO",
    "CATENA_BACKUP_CANDIDATE_ACCESS_KEY",
    "CATENA_BACKUP_CANDIDATE_SECRET_KEY",
)


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


def test_sshd_acceptenv_lists_the_backup_candidate_env_vars():
    passthrough = _defaults()["catena_admin_dispatch_env_passthrough"]
    for var in CANDIDATE_ENV_VARS:
        assert var in passthrough, (
            f"{var} missing from catena_admin_dispatch_env_passthrough -- "
            "sshd would drop it and the candidate would arrive empty"
        )


def test_the_candidate_env_survives_sudo_as_well_as_sshd():
    """Two hops, not one. sshd AcceptEnv lets the var into the SSH session;
    the forced command then runs the dispatcher under `sudo -n`, and sudo's
    env_reset drops the whole environment unless env_keep names the var.

    A var whitelisted on only one hop fails silently in the worst way: the
    action still runs, reads an empty string, and reports on nothing. That is
    how a valid endpoint gets rejected -- the check never saw it."""
    text = HOST.read_text()
    assert "env_keep" in text, (
        "the sudoers drop-in has no env_keep, so sudo's env_reset discards "
        "every candidate credential before the dispatcher reads it"
    )
    # Both hops must render from the same list, or they drift apart and the
    # drift is invisible until a live dispatch.
    assert text.count("catena_admin_dispatch_env_passthrough | join(' ')") == 2, (
        "AcceptEnv and env_keep must both render from "
        "catena_admin_dispatch_env_passthrough"
    )


def test_the_passthrough_list_stays_minimal():
    """Every name is a value the container can push into a root-run host
    process, so the list is the minimum the dispatch needs -- not a general
    env channel."""
    passthrough = _defaults()["catena_admin_dispatch_env_passthrough"]
    assert set(passthrough) == {
        "X_FORWARDED_EMAIL",
        "CATENA_CF_CANDIDATE_TOKEN",
        *CANDIDATE_ENV_VARS,
    }


def test_this_role_installs_no_dispatch_binary_of_its_own():
    """Every binary a dispatch action runs comes out of the image, installed by
    roles/payload at role 5.5 from its blanket `for f in bin/*` loop.

    This role used to install three by hand -- the store writer, the restic-key
    helper, and before them the check binary -- each one a second writer of a
    path roles/payload also writes, with the loser being whichever image was
    staler. The two catena-ce scripts now ride into the image through the
    vendored tree and are installed with the rest, which is what lets the panel
    image authorise the commands that run them."""
    text = HOST.read_text()
    for source in ("helpers/onbox_config.py", "scripts/catena-restic-key.py"):
        assert "src: " not in text.split(source)[0][-40:], (
            f"{source} is copied by this role again; roles/payload installs it "
            "from the image, and two writers of one path means the loser is "
            "whichever image is staler"
        )
    assert "/usr/local/bin/" not in text, (
        "this role writes into /usr/local/bin, which roles/payload owns"
    )
    # The dispatcher is the one executable this role still installs, and it
    # lives in /usr/local/sbin because it is the converge's, not the payload's.
    assert "catena_admin_dispatcher_path" in text
