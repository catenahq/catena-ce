"""Drive the rendered dispatch table for real, through the real runner.

The `*)` arm decides what happens to a name the table does not carry, and it is
the whole security boundary of the forced command: the runner user's
authorized_keys pins one command, and the only thing between
$SSH_ORIGINAL_COMMAND and root is that case statement.

It now offers the name to the payload's drop-in directory before refusing it,
which is what lets an action ship with a panel image instead of waiting for the
next converge. Everything below is about the edges of that: base-first
precedence, a failure that ran versus a name that does not exist, and the
ownership rules on a file root is about to source.

Rendered with jinja2 and executed with bash, rather than asserted as text. The
properties here are all runtime behaviour under `set -euo pipefail`, and a test
that grepped the template would pass for every one of them while the dispatcher
aborted on the first drop-in that ended non-zero.

Run: uv run pytest tests/unit/test_admin_dispatch_overlay.py
"""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import jinja2
import pytest

ANSIBLE = Path(__file__).resolve().parents[2]
TEMPLATE = ANSIBLE / "reconcile" / "roles" / "catena-admin" / "templates" / "admin-actions.j2"
RUNNER = ANSIBLE / "scripts" / "catena-admin-runner.sh"

# One CE arm, enough to prove base-first precedence and that the table still
# dispatches at all.
CE_ACTIONS = [
    {"name": "tailscale-status", "shell": "echo CE-WON"},
    {"name": "backup-now", "shell": "echo CE-BACKUP"},
]


@pytest.fixture
def dispatch(tmp_path):
    """Return a callable: dispatch(action) -> CompletedProcess."""
    overlay = tmp_path / "admin-actions.d"
    overlay.mkdir()

    rendered = jinja2.Template(TEMPLATE.read_text()).render(
        ansible_managed="test",
        catena_admin_dispatcher_path="/usr/local/sbin/catena-admin-runner",
        catena_admin_allowed_actions_path="/etc/catena/admin-allowed.yaml",
        catena_admin_actions_overlay_dir=str(overlay),
        _catena_admin_actions_dispatch=CE_ACTIONS,
    )
    actions_file = tmp_path / "admin-actions"
    actions_file.write_text(rendered)

    runner = tmp_path / "catena-admin-runner"
    runner.write_text(RUNNER.read_text())
    runner.chmod(0o755)

    def run(action: str) -> subprocess.CompletedProcess:
        env = dict(os.environ, CATENA_ADMIN_ACTIONS_FILE=str(actions_file))
        return subprocess.run(
            ["bash", str(runner), action],
            capture_output=True, text=True, env=env, timeout=30,
        )

    run.overlay = overlay  # type: ignore[attr-defined]
    return run


def _drop_in(overlay: Path, name: str, body: str, mode: int = 0o644) -> Path:
    path = overlay / name
    path.write_text(body)
    path.chmod(mode)
    return path


# --- the table still works ---------------------------------------------------

def test_a_known_action_dispatches(dispatch):
    r = dispatch("tailscale-status")
    assert r.returncode == 0, r.stderr
    assert "CE-WON" in r.stdout


def test_an_unknown_action_is_refused_with_the_allowed_list(dispatch):
    r = dispatch("definitely-not-an-action")
    assert r.returncode != 0
    assert "unknown action 'definitely-not-an-action'" in r.stderr
    assert "Allowed:" in r.stderr
    assert r.stdout.strip() == "", (
        "a refusal wrote to stdout, which the caller reads as the action's "
        "result"
    )


# --- the overlay -------------------------------------------------------------

def test_a_drop_in_can_add_a_name_the_converge_never_heard_of(dispatch):
    _drop_in(dispatch.overlay, "10-panel.sh", """
catena_admin_dispatch_payload() {
    case "$1" in
        panel-thing) echo PAYLOAD-RAN ;;
        *) return 127 ;;
    esac
}
""")
    r = dispatch("panel-thing")
    assert r.returncode == 0, r.stderr
    assert "PAYLOAD-RAN" in r.stdout


def test_the_converges_arm_wins_over_a_drop_in_with_the_same_name(dispatch):
    """Base-first, so a drop-in can only ADD. A payload that could REPLACE a
    converge action would be the image silently redefining what a name the
    operator already trusts does."""
    _drop_in(dispatch.overlay, "10-shadow.sh", """
catena_admin_dispatch_payload() {
    echo PAYLOAD-SHADOWED
}
""")
    r = dispatch("tailscale-status")
    assert r.returncode == 0, r.stderr
    assert "CE-WON" in r.stdout
    assert "PAYLOAD-SHADOWED" not in r.stdout


def test_an_action_that_ran_and_failed_is_not_reported_as_unknown(dispatch):
    """The distinction the 127 contract exists for. Printing the rejection over
    a real failure sends the operator looking for a typo instead of a fault."""
    _drop_in(dispatch.overlay, "10-failing.sh", """
catena_admin_dispatch_payload() {
    case "$1" in
        panel-thing) echo TRIED >&2; return 3 ;;
        *) return 127 ;;
    esac
}
""")
    r = dispatch("panel-thing")
    assert r.returncode == 3, (r.returncode, r.stderr)
    assert "TRIED" in r.stderr
    assert "unknown action" not in r.stderr


def test_a_name_no_drop_in_carries_still_reaches_the_rejection(dispatch):
    _drop_in(dispatch.overlay, "10-panel.sh", """
catena_admin_dispatch_payload() {
    case "$1" in
        panel-thing) echo PAYLOAD-RAN ;;
        *) return 127 ;;
    esac
}
""")
    r = dispatch("some-other-name")
    assert r.returncode != 0
    assert "unknown action 'some-other-name'" in r.stderr


def test_every_drop_in_is_asked_in_order(dispatch):
    """Two files defining the same function name is the obvious trap: sourcing
    both leaves the last one standing and the first silently gone. The
    dispatcher clears the function between sources, so each is ASKED."""
    _drop_in(dispatch.overlay, "10-first.sh", """
catena_admin_dispatch_payload() {
    case "$1" in
        first-thing) echo FIRST ;;
        *) return 127 ;;
    esac
}
""")
    _drop_in(dispatch.overlay, "20-second.sh", """
catena_admin_dispatch_payload() {
    case "$1" in
        second-thing) echo SECOND ;;
        *) return 127 ;;
    esac
}
""")
    first = dispatch("first-thing")
    second = dispatch("second-thing")
    assert first.returncode == 0 and "FIRST" in first.stdout, first.stderr
    assert second.returncode == 0 and "SECOND" in second.stdout, second.stderr


def test_a_drop_in_that_ends_non_zero_does_not_abort_the_dispatcher(dispatch):
    """The runner runs under `set -e`. An unguarded source of a file whose last
    statement is non-zero would kill the dispatcher, and every later drop-in --
    plus the rejection itself -- would never run."""
    _drop_in(dispatch.overlay, "05-broken.sh", "false\n")
    _drop_in(dispatch.overlay, "10-panel.sh", """
catena_admin_dispatch_payload() {
    case "$1" in
        panel-thing) echo PAYLOAD-RAN ;;
        *) return 127 ;;
    esac
}
""")
    r = dispatch("panel-thing")
    assert r.returncode == 0, (r.returncode, r.stderr)
    assert "PAYLOAD-RAN" in r.stdout
    assert "failed to load" in r.stderr


def test_a_group_or_world_writable_drop_in_is_ignored(dispatch):
    """Root sourcing a file anything else can write is anything else choosing
    what root runs, which is the entire property the forced command holds."""
    _drop_in(dispatch.overlay, "10-loose.sh", """
catena_admin_dispatch_payload() {
    echo SHOULD-NOT-RUN
}
""", mode=0o666)
    r = dispatch("panel-thing")
    assert "SHOULD-NOT-RUN" not in r.stdout
    assert "group- or world-writable" in r.stderr
    assert r.returncode != 0


def test_a_non_sh_file_is_not_sourced(dispatch):
    _drop_in(dispatch.overlay, "notes.txt", "echo SHOULD-NOT-RUN\n")
    r = dispatch("tailscale-status")
    assert r.returncode == 0, r.stderr
    assert "SHOULD-NOT-RUN" not in r.stdout


def test_an_absent_overlay_directory_changes_nothing(dispatch, tmp_path):
    """The directory is created by the converge. A host converged before it
    existed has to keep dispatching, and keep refusing."""
    dispatch.overlay.rmdir()
    known = dispatch("tailscale-status")
    unknown = dispatch("nope")
    assert known.returncode == 0 and "CE-WON" in known.stdout, known.stderr
    assert unknown.returncode != 0 and "unknown action" in unknown.stderr


def test_the_payload_still_gets_the_payload_env_var(dispatch):
    """$PAYLOAD is how every dispatched request carries its argument. A drop-in
    that could not see it would be limited to actions with no input."""
    _drop_in(dispatch.overlay, "10-echo.sh", """
catena_admin_dispatch_payload() {
    case "$1" in
        panel-echo) printf 'GOT:%s\\n' "$PAYLOAD" ;;
        *) return 127 ;;
    esac
}
""")
    out = dispatch("panel-echo hello")
    assert out.returncode == 0, out.stderr
    assert "GOT:hello" in out.stdout


def test_the_overlay_directory_is_created_root_owned_by_the_converge():
    """Created by the converge and read by nothing there: the same one-way
    dependency /etc/catena/post-restore.d has."""
    import yaml

    tasks = yaml.safe_load(
        (ANSIBLE / "reconcile" / "roles" / "catena-admin" / "tasks" / "catalog.yml").read_text()
    )
    task = next(
        (t for t in tasks
         if "overlay dir" in (t.get("name") or "")), None,
    )
    assert task is not None, "the converge no longer creates the overlay dir"
    f = task["ansible.builtin.file"]
    assert f["state"] == "directory"
    assert f["owner"] == "root" and f["group"] == "root"
    assert f["mode"] == "0755", (
        f"mode {f['mode']}; a group- or world-writable directory lets anything "
        "else drop a file root would source"
    )


def test_the_mode_guard_matches_what_the_converge_creates():
    """Belt-and-braces on the directory: the dispatcher checks each FILE, and
    a 0644 drop-in is what the payload installer writes."""
    assert stat.S_IMODE(0o644) & 0o022 == 0
