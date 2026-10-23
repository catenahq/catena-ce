"""The sync-all action waits for a sync run already in flight to end, starts a
fresh one and reports that one: systemd joins a start of an active oneshot,
so a bare `start --wait` can report a run that began before the press. It
holds the host lock for its run, and answers 75, busy, while another job
holds it.

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


def _shell(lock) -> str:
    """The action's shell, with the host lock pointed at the test's own file."""
    text = jinja2.Template(TEMPLATE.read_text()).render(ansible_managed="")
    for a in yaml.safe_load(text)["actions"]:
        if a["name"] == "sync-all":
            return a["shell"].replace("/run/catena.lock", str(lock))
    raise AssertionError("sync-all not found")


def _press(tmp_path, in_flight: bool):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "calls"
    state = tmp_path / "state"
    state.write_text("activating" if in_flight else "inactive")
    idle = tmp_path / "idle"
    idle.write_text("inactive")
    # is-active reports the in-flight dashboard-sync run once more, then the
    # run is over; gatus-sync is idle.
    (bindir / "systemctl").write_text(
        "#!/bin/bash\n"
        f'echo "systemctl $*" >> "{log}"\n'
        'if [ "$1" = is-active ]; then\n'
        f'  f="{state}"; [ "$2" = catena-dashboard-sync.service ] || f="{idle}"\n'
        '  cat "$f"; echo inactive > "$f"\n'
        "fi\n"
        'if [ "$1" = show ]; then echo "inv-$5"; fi\n'
    )
    (bindir / "journalctl").write_text(
        f'#!/bin/bash\necho "journalctl $*" >> "{log}"\necho "journal of $*"\n')
    (bindir / "sleep").write_text("#!/bin/bash\nexit 0\n")
    for f in bindir.iterdir():
        f.chmod(0o755)
    r = subprocess.run(
        ["bash", "-c", "set -euo pipefail\n" + _shell(tmp_path / "lock")],
        capture_output=True, text=True, timeout=30,
        env=dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}"),
    )
    return r, log.read_text().splitlines()


def test_a_run_in_flight_is_waited_out_before_the_fresh_start(tmp_path):
    r, calls = _press(tmp_path, in_flight=True)
    assert r.returncode == 0, r.stderr
    dash = [c for c in calls if "catena-dashboard-sync.service" in c and "journalctl" not in c]
    assert dash == [
        "systemctl is-active catena-dashboard-sync.service",
        "systemctl is-active catena-dashboard-sync.service",
        "systemctl start --wait catena-dashboard-sync.service",
        "systemctl show -p InvocationID --value catena-dashboard-sync.service",
    ]


def test_an_idle_unit_is_started_once(tmp_path):
    r, calls = _press(tmp_path, in_flight=False)
    assert r.returncode == 0, r.stderr
    assert calls.count("systemctl start --wait catena-dashboard-sync.service") == 1
    assert calls.count("systemctl start --wait gatus-sync.service") == 1


def test_the_log_shown_is_the_fresh_runs_journal_alone(tmp_path):
    """The journal is selected by the invocation id read after the start
    returns, so it holds the fresh run alone, never the tail of a run the
    start waited for."""
    r, calls = _press(tmp_path, in_flight=True)
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
    for f in bindir.iterdir():
        f.chmod(0o755)
    r = subprocess.run(
        ["bash", "-c", "set -euo pipefail\n" + _shell(tmp_path / "lock")],
        capture_output=True, text=True, timeout=30,
        env=dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}"),
    )
    assert r.returncode != 0
    assert "dashboard-sync=0 gatus-sync=1" in r.stdout
    assert "journal of" in r.stdout


def _press_while_another_job_holds_the_lock(tmp_path, hold: bool):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "calls"
    (bindir / "systemctl").write_text(
        f'#!/bin/bash\necho "systemctl $*" >> "{log}"\n'
        '[ "$1" = show ] && echo inv-x\nexit 0\n')
    (bindir / "journalctl").write_text('#!/bin/bash\necho "journal of $*"\n')
    for f in bindir.iterdir():
        f.chmod(0o755)
    lock = tmp_path / "lock"
    holder = open(lock, "w")
    if hold:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        r = subprocess.run(
            ["bash", "-c", "set -euo pipefail\n" + _shell(lock)],
            capture_output=True, text=True, timeout=30,
            env=dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}"),
        )
    finally:
        holder.close()
    return r, log


def test_a_press_while_another_job_holds_the_lock_answers_busy_and_syncs_nothing(tmp_path):
    r, log = _press_while_another_job_holds_the_lock(tmp_path, hold=True)
    assert r.returncode == 75, r.stderr
    assert "The server is busy with another job" in r.stdout
    assert "Le serveur est occup" in r.stdout
    assert not log.exists(), "a sync started without the lock"


def test_a_press_with_the_lock_free_holds_it_for_its_run_and_lets_go(tmp_path):
    r, log = _press_while_another_job_holds_the_lock(tmp_path, hold=False)
    assert r.returncode == 0, r.stderr
    assert "start --wait catena-dashboard-sync.service" in log.read_text()
    with open(tmp_path / "lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
