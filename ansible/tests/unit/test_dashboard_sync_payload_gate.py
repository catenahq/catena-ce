"""The dashboard-sync wiring depends on a file this repo no longer ships.

catena-dashboard-sync and its lib modules moved into the catena-admin payload,
so roles/infrastructure now wires a timer around a binary roles/payload put on
the host five roles earlier. Whether its absence is a defect depends on whether
THIS converge was the one that had to install it -- the same two-legged shape
roles/cloudflare_tunnel and roles/backup already use.

These pin that: a converge that owns the engines fails loudly, a converge that
does not defers, and nothing that RUNS the reconciler is attempted in the
deferred case. That last one is the trap: a .timer whose .service is absent is
not inert, systemd refuses to start it, so an ungated enable would turn the
deferral into the failure it exists to avoid.

Run: uv run pytest tests/unit/test_dashboard_sync_payload_gate.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

TASKS = (
    Path(__file__).resolve().parents[3]
    / "ansible" / "roles" / "infrastructure" / "tasks" / "dashboard_sync.yml"
)

_EXPECTED = "catena_payload_engines_expected"
_STAT = "_dashboard_sync_bin.stat.exists"


def _tasks() -> list[dict]:
    return yaml.safe_load(TASKS.read_text())


def _find(name_fragment: str) -> dict:
    for task in _tasks():
        if name_fragment in (task.get("name") or ""):
            return task
    raise AssertionError(f"task not found: {name_fragment!r}")


def _cond(task: dict) -> str:
    when = task.get("when")
    if when is None:
        return ""
    return " ".join(str(c) for c in when) if isinstance(when, list) else str(when)


def test_the_reconciler_is_stat_ed_before_anything_uses_it():
    task = _find("the reconciler is on this host")
    assert task["ansible.builtin.stat"]["path"] == "{{ dashboard_sync_script_path }}"
    assert task["register"] == "_dashboard_sync_bin"


def test_a_missing_reconciler_fails_a_converge_that_installs_it():
    """Production. roles/payload ran, the file should be there, and a timer
    firing at a path that does not exist shows up as apps silently losing their
    routes rather than as a failed converge."""
    task = _find("missing from a converge that installs it")
    assert "ansible.builtin.fail" in task
    cond = _cond(task)
    assert _EXPECTED in cond
    assert f"not ({_STAT}" in cond


def test_a_host_with_out_of_band_engines_defers_instead():
    """The bench, and any host where something else owns /usr/local/bin/catena-*.
    The converge asserting on another manager's property is the two-managers
    mistake in miniature."""
    task = _find("staged out of band")
    assert "ansible.builtin.debug" in task
    cond = _cond(task)
    assert f"not ({_EXPECTED}" in cond
    assert f"not ({_STAT}" in cond


def test_the_two_legs_cannot_both_fire_or_both_stay_silent():
    """They partition the missing-reconciler case. Two conditions that could
    both be false would restore the silent skip this pair replaced."""
    fail_cond = _cond(_find("missing from a converge that installs it"))
    defer_cond = _cond(_find("staged out of band"))
    assert (_EXPECTED in fail_cond) and (f"not ({_EXPECTED}" not in fail_cond)
    assert f"not ({_EXPECTED}" in defer_cond


def test_everything_that_runs_the_reconciler_is_gated_on_it_existing():
    """The deferred case must reach the end of the file without touching
    systemd. `systemctl start` of a .timer whose .service is absent fails, and
    the run-once task would start a unit that does not exist."""
    runs_it = [
        "systemd timer unit",
        "enable + start timer",
        "run once now",
    ]
    for fragment in runs_it:
        cond = _cond(_find(fragment))
        assert _STAT in cond, (
            f"{fragment!r} is not gated on the reconciler existing; on a host "
            f"whose engines are staged out of band this is what fails the "
            f"converge the deferral above exists to avoid"
        )


def test_the_env_file_is_written_unconditionally():
    """It is the one artifact here whose correct value changes with the HOST,
    not with the payload -- the Portainer key, the oauth2-proxy client secret,
    the per-zone cookie secrets. Withholding it until the payload lands would
    make the next converge's first act a secret rotation."""
    task = _find("env file")
    assert _cond(task) == ""
    assert task["no_log"] is True


def test_the_daemon_reload_survives_a_skipped_template():
    """`_dashboard_sync_timer_unit` is a skipped register in the deferred case.
    Reading .changed off it without a default is an undefined-attribute error,
    which fails the converge for the reason the gate exists to prevent."""
    cond = _cond(_find("daemon-reload if the timer changed"))
    assert "default(false)" in cond
