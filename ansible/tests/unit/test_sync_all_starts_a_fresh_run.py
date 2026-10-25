"""The sync-all action waits for a sync run already in flight to end, starts a
fresh one and reports that one: systemd joins a start of an active oneshot,
so a bare `start --wait` can report a run that began before the press. The
dashboard sync starts under the host lock, lent to its unit; gatus-sync starts
beside it with no lock. A press answers 75, busy, while another job holds the
lock.

Run: uv run pytest tests/unit/test_sync_all_starts_a_fresh_run.py
"""
from __future__ import annotations

import fcntl
import os
import subprocess

import jinja2
import yaml

from ansible_tree import role_dir

TEMPLATE = role_dir("catena-admin") / "templates" / "actions.yml.j2"
LEND = "host-lock -w 0 -lend catena-dashboard-sync.service --"


def _shell(recovery) -> str:
    """The action's shell, with catena-recovery pointed at the test's stand-in."""
    text = jinja2.Template(TEMPLATE.read_text()).render(ansible_managed="")
    for a in yaml.safe_load(text)["actions"]:
        if a["name"] == "sync-all":
            assert "/usr/local/bin/catena-recovery" in a["shell"]
            return a["shell"].replace("/usr/local/bin/catena-recovery", str(recovery))
    raise AssertionError("sync-all not found")


def _press(tmp_path, in_flight: bool = False, hold: bool = False):
    """Runs the action against stand-ins. The recovery stand-in checks the
    lend arguments, then runs the command under a non-blocking flock of the
    test's lock file, answering 75 when it is held, as host-lock -w 0 does."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "calls"
    lock = tmp_path / "lock"
    state = tmp_path / "state"
    state.write_text("activating" if in_flight else "inactive")
    idle = tmp_path / "idle"
    idle.write_text("inactive")
    # is-active reports the in-flight dashboard-sync run once more, then the
    # run is over; gatus-sync is idle. A start records whether the lock is
    # held at that moment.
    (bindir / "systemctl").write_text(
        "#!/bin/bash\n"
        f'echo "systemctl $*" >> "{log}"\n'
        'if [ "$1" = is-active ]; then\n'
        f'  f="{state}"; [ "$2" = catena-dashboard-sync.service ] || f="{idle}"\n'
        '  cat "$f"; echo inactive > "$f"\n'
        "fi\n"
        'if [ "$1" = start ]; then\n'
        f'  if [ -e "{tmp_path}/inhold" ]; then h=held; else h=free; fi\n'
        f'  echo "lock $3 $h" >> "{log}"\n'
        "fi\n"
        'if [ "$1" = show ]; then echo "inv-$5"; fi\n'
    )
    (bindir / "journalctl").write_text(
        f'#!/bin/bash\necho "journalctl $*" >> "{log}"\necho "journal of $*"\n')
    (bindir / "sleep").write_text("#!/bin/bash\nexit 0\n")
    recovery = bindir / "catena-recovery"
    recovery.write_text(
        "#!/bin/bash\n"
        f'echo "recovery $*" >> "{log}"\n'
        f'[ "${{*:1:6}}" = "{LEND}" ] || exit 2\n'
        "shift 6\n"
        f"flock -n -E 75 '{lock}' bash -c 'touch \"{tmp_path}/inhold\"; \"$@\"; rc=$?; "
        f"rm -f \"{tmp_path}/inhold\"; exit $rc' x \"$@\"\n"
        "rc=$?\n"
        f'echo "recovery done $rc" >> "{log}"\n'
        "exit $rc\n"
    )
    for f in bindir.iterdir():
        f.chmod(0o755)
    holder = open(lock, "w")
    if hold:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        r = subprocess.run(
            ["bash", "-c", "set -euo pipefail\n" + _shell(recovery)],
            capture_output=True, text=True, timeout=30,
            env=dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}"),
        )
    finally:
        holder.close()
    return r, log.read_text().splitlines(), lock


def test_a_run_in_flight_is_waited_out_before_the_fresh_start(tmp_path):
    r, calls, _ = _press(tmp_path, in_flight=True)
    assert r.returncode == 0, r.stderr
    dash = [c for c in calls if c.startswith("systemctl") and "catena-dashboard-sync.service" in c]
    assert dash == [
        "systemctl is-active catena-dashboard-sync.service",
        "systemctl is-active catena-dashboard-sync.service",
        "systemctl start --wait catena-dashboard-sync.service",
        "systemctl show -p InvocationID --value catena-dashboard-sync.service",
    ]


def test_an_idle_unit_is_started_once(tmp_path):
    r, calls, _ = _press(tmp_path)
    assert r.returncode == 0, r.stderr
    assert calls.count("systemctl start --wait catena-dashboard-sync.service") == 1
    assert calls.count("systemctl start --wait gatus-sync.service") == 1


def test_the_log_shown_is_the_fresh_runs_journal_alone(tmp_path):
    """The journal is selected by the invocation id read after the start
    returns, so it holds the fresh run alone, never the tail of a run the
    start waited for."""
    r, calls, _ = _press(tmp_path, in_flight=True)
    journals = [c for c in calls if c.startswith("journalctl")]
    assert sorted(journals) == sorted([
        "journalctl _SYSTEMD_INVOCATION_ID=inv-catena-dashboard-sync.service --no-pager -n 80",
        "journalctl _SYSTEMD_INVOCATION_ID=inv-gatus-sync.service --no-pager -n 80",
    ])
    assert not any("--since" in c or " -u " in c for c in journals)
    for unit in ("catena-dashboard-sync.service", "gatus-sync.service"):
        assert calls.index(f"systemctl show -p InvocationID --value {unit}") > calls.index(
            f"systemctl start --wait {unit}")
    assert "journal of _SYSTEMD_INVOCATION_ID=inv-gatus-sync.service" in r.stdout
    assert "dashboard-sync=0 gatus-sync=0" in r.stdout


def test_a_failed_sync_fails_the_action_and_keeps_its_log(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "systemctl").write_text(
        '#!/bin/bash\n[ "$1" = start ] && [ "$3" = gatus-sync.service ] && exit 1\n'
        '[ "$1" = show ] && echo inv-x\nexit 0\n')
    (bindir / "journalctl").write_text('#!/bin/bash\necho "journal of $*"\n')
    (bindir / "catena-recovery").write_text('#!/bin/bash\nshift 6\n"$@"\n')
    for f in bindir.iterdir():
        f.chmod(0o755)
    r = subprocess.run(
        ["bash", "-c", "set -euo pipefail\n" + _shell(bindir / "catena-recovery")],
        capture_output=True, text=True, timeout=30,
        env=dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}"),
    )
    assert r.returncode != 0
    assert "dashboard-sync=0 gatus-sync=1" in r.stdout
    assert "journal of" in r.stdout


def test_a_press_while_another_job_holds_the_lock_answers_busy_and_starts_no_dashboard_sync(tmp_path):
    r, calls, _ = _press(tmp_path, hold=True)
    assert r.returncode == 75, r.stderr
    assert "The server is busy with another job" in r.stdout
    assert "the applications were not synchronised" in r.stdout
    assert "Le serveur est occup" in r.stdout
    assert "les applications n'ont pas été synchronisées" in r.stdout
    assert "systemctl start --wait catena-dashboard-sync.service" not in calls
    assert "journal of" not in r.stdout


def test_the_dashboard_sync_starts_inside_the_hold_lent_to_it(tmp_path):
    r, calls, lock = _press(tmp_path)
    assert r.returncode == 0, r.stderr
    assert f"recovery {LEND} systemctl start --wait catena-dashboard-sync.service" in calls
    assert "lock catena-dashboard-sync.service held" in calls
    assert "lock gatus-sync.service free" in calls
    assert calls.index("recovery done 0") > calls.index("lock catena-dashboard-sync.service held")
    with open(lock, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
