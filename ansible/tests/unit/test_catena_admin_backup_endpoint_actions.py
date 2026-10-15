"""The env channel the backup-endpoint (S3) candidate check travels through.

A candidate S3 repository plus access/secret key is validated BEFORE the
Settings panel persists it. The three values ride the SSH Runner's env map,
never $PAYLOAD/argv, so they never land in $SSH_ORIGINAL_COMMAND or any host
process argv -- and a name that disagrees on any hop silently breaks the whole
feature: the action sees an empty candidate and the panel is told a valid
endpoint is invalid.

The ACTION is authorised by the panel image's dispatch drop-in, with the other
Community ones, and what it has to look like is asserted there (catena-admin
payload/actions.d/catena_actions_test.go). What stays here is the part that is
about this host rather than about the command: sshd's AcceptEnv and the sudoers
env_keep, which are converge-owned permanently (reconcile/roles/dispatch_path).
The image's drop-ins can add a dispatch arm; the set of inputs root will
accept is not a decision they get to make.

Run: uv run pytest tests/unit/test_catena_admin_backup_endpoint_actions.py
"""
from __future__ import annotations

import sys

from ansible_tree import ANSIBLE, PANEL_HOST_TASKS as HOST, panel_vars

sys.path.insert(0, str(ANSIBLE / "helpers"))

import dispatch_path  # noqa: E402

CANDIDATE_ENV_VARS = (
    "CATENA_BACKUP_CANDIDATE_REPO",
    "CATENA_BACKUP_CANDIDATE_ACCESS_KEY",
    "CATENA_BACKUP_CANDIDATE_SECRET_KEY",
)


def test_sshd_acceptenv_lists_the_backup_candidate_env_vars():
    passthrough = panel_vars()["catena_admin_dispatch_env_passthrough"]
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
    how a valid endpoint gets rejected -- the check never saw it. Both hops
    render from the one list, and the dispatch path's proof refuses a
    generation that one of them drops."""
    passthrough = panel_vars()["catena_admin_dispatch_env_passthrough"]
    names = " ".join(passthrough)
    sudo = dispatch_path.sudo_rule("runner", "/x/dispatcher", passthrough)
    accept = dispatch_path.acceptenv_rule("runner", passthrough)
    assert f'Defaults:runner env_keep += "{names}"' in sudo, (
        "the sudo rule does not keep every passthrough variable, so sudo's "
        "env_reset discards the candidate credentials before the dispatcher "
        "reads them"
    )
    assert f"    AcceptEnv {names}\n" in accept
    for var in CANDIDATE_ENV_VARS:
        assert var in passthrough


def test_the_passthrough_list_stays_minimal():
    """Every name is a value the container can push into a root-run host
    process, so the list is the minimum the dispatch needs -- not a general
    env channel."""
    passthrough = panel_vars()["catena_admin_dispatch_env_passthrough"]
    assert set(passthrough) == {
        "X_FORWARDED_EMAIL",
        "CATENA_DISPATCH_PAYLOAD",
        "CATENA_CF_CANDIDATE_TOKEN",
        *CANDIDATE_ENV_VARS,
    }


def test_this_role_installs_no_dispatch_binary_of_its_own():
    """Every binary a dispatch action runs comes out of the image, installed by
    reconcile/roles/payload from its blanket `for f in bin/*` loop.

    A hand-installed one here -- the store writer, the restic-key helper, a
    check binary -- makes this role a second writer of a path
    reconcile/roles/payload also writes, and the loser is whichever image is
    staler. The two catena-ce scripts ride into the image through the
    vendored tree and are installed with the rest, which is what lets the panel
    image authorise the commands that run them. The dispatcher and the forced
    command are reconcile/roles/dispatch_path's, outside /usr/local/bin."""
    text = HOST.read_text()
    for source in ("helpers/onbox_config.py", "scripts/catena-restic-key.py"):
        assert "src: " not in text.split(source)[0][-40:], (
            f"{source} is copied by this role again; reconcile/roles/payload installs it "
            "from the image, and two writers of one path means the loser is "
            "whichever image is staler"
        )
    assert "/usr/local/bin/" not in text, (
        "this role writes into /usr/local/bin, which reconcile/roles/payload owns"
    )
    assert "catena_admin_dispatcher_path" not in text, (
        "this role installs the dispatcher again, beside the dispatch path's "
        "proven switch"
    )
    assert not dispatch_path.GENERATIONS.startswith("/usr/local/bin/")
    assert not panel_vars()["catena_admin_dispatcher_path"].startswith("/usr/local/bin/")
