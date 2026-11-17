"""The cloudflared-restart action runs under the host lock and answers 75,
busy, in English and French while another job holds the lock.

Run: uv run pytest tests/unit/test_cloudflared_restart_takes_the_host_lock.py
"""
from __future__ import annotations

import fcntl
import os
import subprocess

import jinja2
import yaml

from ansible_tree import role_dir

TEMPLATE = role_dir("catena-admin") / "templates" / "actions.yml.j2"


def _shell(recovery) -> str:
    text = jinja2.Template(TEMPLATE.read_text()).render(ansible_managed="")
    for a in yaml.safe_load(text)["actions"]:
        if a["name"] == "cloudflared-restart":
            assert "/usr/local/bin/catena-recovery" in a["shell"]
            return a["shell"].replace("/usr/local/bin/catena-recovery", str(recovery))
    raise AssertionError("cloudflared-restart not found")


def _press(tmp_path, hold: bool):
    """The recovery stand-in checks the host-lock arguments, then runs the
    command under a non-blocking flock of the test's lock file, answering 75
    when it is held, as host-lock -w 0 does."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "calls"
    lock = tmp_path / "lock"
    (bindir / "docker").write_text(f'#!/bin/bash\necho "docker $*" >> "{log}"\n')
    recovery = bindir / "catena-recovery"
    recovery.write_text(
        "#!/bin/bash\n"
        '[ "${*:1:3}" = "host-lock -w 0" ] && [ "$4" = -- ] || exit 2\n'
        "shift 4\n"
        f"exec flock -n -E 75 '{lock}' \"$@\"\n"
    )
    for f in bindir.iterdir():
        f.chmod(0o755)
    log.touch()
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
    return r, log.read_text().splitlines()


def test_a_press_restarts_cloudflared_under_the_lock(tmp_path):
    r, calls = _press(tmp_path, hold=False)
    assert r.returncode == 0, r.stderr
    assert calls == ["docker service update --force cloudflared"]


def test_a_press_while_another_job_holds_the_lock_answers_busy(tmp_path):
    r, calls = _press(tmp_path, hold=True)
    assert r.returncode == 75, r.stderr
    assert "The server is busy with another job" in r.stdout
    assert "Le serveur est occup" in r.stdout
    assert calls == []
