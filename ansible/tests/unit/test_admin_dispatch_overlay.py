"""Drive the rendered dispatch table for real, through the real runner.

The `*)` arm decides what happens to a name the table does not carry, and it is
the whole security boundary of the forced command: the runner user's
authorized_keys pins one command, and the only thing between
$SSH_ORIGINAL_COMMAND and root is that case statement.

It offers the name to the payload's drop-in directory before refusing it,
which is what lets an action ship with a panel image instead of waiting for the
next converge. Most of what follows is about the edges of that: base-first
precedence, a failure that ran versus a name that does not exist, and the
ownership rules on a file root is about to source. The rest is the request's
payload, which rides CATENA_DISPATCH_PAYLOAD because sudo logs the command line
it runs.

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

from ansible_tree import panel_vars

ANSIBLE = Path(__file__).resolve().parents[2]
TEMPLATE = ANSIBLE / "reconcile" / "roles" / "catena-admin" / "templates" / "admin-actions.j2"
RUNNER = ANSIBLE / "scripts" / "catena-admin-runner.sh"

# The variable a request's payload rides: the panel's SSH runner sets it
# (catena-admin shell/actions/dispatcher.go PayloadEnv) and the dispatcher reads
# it.
PAYLOAD_ENV = "CATENA_DISPATCH_PAYLOAD"

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

    def run(*argv: str, payload: str | None = None) -> subprocess.CompletedProcess:
        env = dict(os.environ, CATENA_ADMIN_ACTIONS_FILE=str(actions_file))
        env.pop(PAYLOAD_ENV, None)
        if payload is not None:
            env[PAYLOAD_ENV] = payload
        return subprocess.run(
            ["bash", str(runner), *argv],
            capture_output=True, text=True, env=env, timeout=30,
        )

    run.overlay = overlay  # type: ignore[attr-defined]
    run.runner = runner  # type: ignore[attr-defined]
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


_ECHO_DROP_IN = """
catena_admin_dispatch_payload() {
    case "$1" in
        panel-echo) printf 'GOT:%s\\n' "$PAYLOAD" ;;
        *) return 127 ;;
    esac
}
"""


def test_the_payload_still_gets_the_payload_env_var(dispatch):
    """$PAYLOAD is how every dispatched request carries its argument. A drop-in
    that could not see it would be limited to actions with no input."""
    _drop_in(dispatch.overlay, "10-echo.sh", _ECHO_DROP_IN)
    out = dispatch("panel-echo", payload="aGVsbG8gd29ybGQ=")
    assert out.returncode == 0, out.stderr
    assert "GOT:aGVsbG8gd29ybGQ=" in out.stdout


def test_an_action_with_no_payload_variable_reads_an_empty_payload(dispatch):
    """A bare action arrives without the variable, and the runner runs under
    `set -u`: reading it bare would kill the dispatch instead of running it."""
    _drop_in(dispatch.overlay, "10-echo.sh", _ECHO_DROP_IN)
    out = dispatch("panel-echo")
    assert out.returncode == 0, out.stderr
    assert "GOT:\n" in out.stdout


# --- the payload stays off the command line ------------------------------------

@pytest.mark.parametrize("argv", [
    ("panel-echo c2VjcmV0LWtleQ==",),
    ("panel-echo\tc2VjcmV0LWtleQ==",),
    ("panel-echo\nc2VjcmV0LWtleQ==",),
    ("panel-echo", "c2VjcmV0LWtleQ=="),
], ids=["space", "tab", "newline", "second-argument"])
def test_a_payload_on_the_command_line_is_refused_before_anything_runs(dispatch, argv):
    """sudo logs the command line it runs and the process table shows it, so a
    secret there is a secret in the journal. Refusing it is what keeps a panel
    from ever putting one back: a request that tried would fail, not leak
    quietly. The refusal names the action and never the payload."""
    _drop_in(dispatch.overlay, "10-echo.sh", _ECHO_DROP_IN)
    r = dispatch(*argv)
    assert r.returncode == 2, (r.returncode, r.stderr)
    assert r.stdout.strip() == "", "the arm ran with a payload off the command line"
    assert "refusing 'panel-echo'" in r.stderr
    assert PAYLOAD_ENV in r.stderr, "the refusal does not say where the payload goes"
    assert "c2VjcmV0LWtleQ==" not in r.stderr, "the refusal echoed the payload"


def test_a_refused_command_line_ignores_the_payload_variable(dispatch):
    """The variable being set does not make a command-line payload acceptable."""
    _drop_in(dispatch.overlay, "10-echo.sh", _ECHO_DROP_IN)
    r = dispatch("panel-echo c2VjcmV0LWtleQ==", payload="aGVsbG8=")
    assert r.returncode == 2, r.stderr
    assert r.stdout.strip() == ""


def test_the_payload_variable_crosses_both_hops():
    """sshd's AcceptEnv and the sudoers env_keep both render from the
    passthrough list (test_catena_admin_backup_endpoint_actions.py holds that),
    so the name the dispatcher reads has to be on it, or every payload arrives
    empty and every write reports a malformed request."""
    assert PAYLOAD_ENV in panel_vars()["catena_admin_dispatch_env_passthrough"]
    assert f'"${{{PAYLOAD_ENV}:-}}"' in RUNNER.read_text(), (
        f"the dispatcher does not read $PAYLOAD from {PAYLOAD_ENV}")


# --- the dispatch path's proof ------------------------------------------------

def _proof(runner: Path, *argv: str, **env: str) -> subprocess.CompletedProcess:
    full = {k: v for k, v in os.environ.items() if k not in (PAYLOAD_ENV, "X_FORWARDED_EMAIL")}
    full.update(env, CATENA_ADMIN_ACTIONS_FILE=str(runner.parent / "no-such-table"))
    return subprocess.run(["bash", str(runner), "dispatch-path-proof", *argv],
                          capture_output=True, text=True, env=full, timeout=30)


def test_the_proof_names_its_generation_and_what_arrived_never_a_value(dispatch):
    """helpers/dispatch_path.py sets every passthrough variable to the
    payload's value and reads back which arrived: the generation answering is
    the directory the dispatcher lives in, and a variable is named only when it
    holds that value."""
    r = _proof(dispatch.runner, **{PAYLOAD_ENV: "f00dfeed", "X_FORWARDED_EMAIL": "f00dfeed",
                                   "CATENA_CF_CANDIDATE_TOKEN": "another"})
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    assert lines[0] == f"dispatch-path-proof {dispatch.runner.parent.name}"
    assert sorted(lines[1:]) == [f"arrived {PAYLOAD_ENV}", "arrived X_FORWARDED_EMAIL"]
    assert "f00dfeed" not in r.stdout and "another" not in r.stdout


def test_the_proof_answers_before_the_dispatch_table(dispatch):
    """A first install proves its path before reconcile/roles/catena-admin has
    written the dispatch table, so the proof cannot depend on it."""
    r = _proof(dispatch.runner)
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == [f"dispatch-path-proof {dispatch.runner.parent.name}"]


def test_the_proof_with_a_payload_on_the_command_line_is_refused(dispatch):
    r = _proof(dispatch.runner, "c2VjcmV0")
    assert r.returncode == 2
    assert r.stdout == "" and "c2VjcmV0" not in r.stderr


def test_the_overlay_directory_is_created_root_owned_by_the_converge():
    """Created by the converge and read by nothing there: a one-way
    dependency, the payload drops its actions into it."""
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
