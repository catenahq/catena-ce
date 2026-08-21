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

The validate half is here too, because it is the SAME property on a second
surface and the first fix missed it: validate.yml asserted the reconciler and
its timer with no gate at all, so a host mid-assembly failed validation for
being mid-assembly.

Run: uv run pytest tests/unit/test_dashboard_sync_payload_gate.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

_ROLE = (
    Path(__file__).resolve().parents[3]
    / "ansible" / "roles" / "infrastructure"
)
TASKS = _ROLE / "tasks" / "dashboard_sync.yml"
VALIDATE = _ROLE / "tasks" / "validate.yml"

_EXPECTED = "catena_payload_engines_expected"
_STAT = "_dashboard_sync_bin.stat.exists"
_V_EXPECTED = "_infra_payload_expected"


def _tasks() -> list[dict]:
    return yaml.safe_load(TASKS.read_text())


def _validate_tasks() -> list[dict]:
    return yaml.safe_load(VALIDATE.read_text())


def _find(name_fragment: str) -> dict:
    for task in _tasks():
        if name_fragment in (task.get("name") or ""):
            return task
    raise AssertionError(f"task not found: {name_fragment!r}")


def _find_v(name_fragment: str) -> dict:
    for task in _validate_tasks():
        if name_fragment in (task.get("name") or ""):
            return task
    raise AssertionError(f"validate task not found: {name_fragment!r}")


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


# --- validate.yml: the same property, the surface the first fix missed ------

def test_validate_decides_from_the_env_not_the_role_fact():
    """validate.yml is a standalone play, so roles/payload's set_fact is not in
    scope. roles/backup/tasks/validate.yml reads the env var for exactly this
    reason; reading the fact here would evaluate undefined every time."""
    task = _find_v("decide whether the payload is expected here")
    expr = str(task["ansible.builtin.set_fact"][_V_EXPECTED])
    assert "CATENA_PAYLOAD_INSTALL" in expr
    assert _EXPECTED not in expr


def test_validate_still_asserts_a_reconciler_that_is_already_present():
    """The 'or it is already here' leg. Without it, a converged host that
    happens to run with the env var set would stop checking a file it has --
    the gate would be a way to switch the assertion off."""
    expr = str(
        _find_v("decide whether the payload is expected here")
        ["ansible.builtin.set_fact"][_V_EXPECTED]
    )
    assert "_infra_dashboard_sync.stat.exists" in expr
    assert " or " in expr


def test_validate_asserts_the_reconciler_only_when_it_is_expected():
    task = _find_v("payload-shipped reconciler is installed")
    assert "ansible.builtin.assert" in task
    assert _V_EXPECTED in _cond(task)


def test_validate_says_so_when_it_defers():
    """'Deferred' and 'checked and fine' must not be the same silence."""
    task = _find_v("reconciler checks deferred")
    assert "ansible.builtin.debug" in task
    assert f"not ({_V_EXPECTED}" in _cond(task)


def test_the_reconciler_left_the_unconditional_fixture_loop():
    """It used to sit in the same loop as gatus-sync and the traefik dynamic
    dir, which is how an ungated assertion survived the move."""
    loop = str(_find_v("stat sync scripts + traefik dynamic dir")["loop"])
    assert "catena-dashboard-sync" not in loop
    assert "gatus-sync" in loop


def test_the_role_owned_fixtures_are_still_asserted_unconditionally():
    """The deferral must not become a way to skip everything. gatus-sync, the
    OIDC wirer, the export dir and the traefik dynamic dir are this role's."""
    for task_name in ("stat sync scripts + traefik dynamic dir",
                      "fixture files/dirs correct"):
        assert _cond(_find_v(task_name)) == "", task_name


def test_the_dashboard_sync_timer_probe_follows_the_same_gate():
    """The converge does not wire a timer whose .service is absent, so probing
    it on a deferred host asserts a timer nothing was supposed to arm."""
    expr = str(_find_v("which sync timers should be armed")
               ["ansible.builtin.set_fact"]["_infra_sync_timers"])
    assert "catena-dashboard-sync.timer" in expr
    assert _V_EXPECTED in expr


def test_the_gatus_timer_is_never_dropped_by_the_gate():
    """gatus-sync is this role's own. If the deferral dropped both timers, a
    host with no payload would have no timer coverage at all -- which reads as
    a pass."""
    expr = str(_find_v("which sync timers should be armed")
               ["ansible.builtin.set_fact"]["_infra_sync_timers"])
    tail = expr.split("else [])", 1)[-1]
    assert "gatus-sync.timer" in tail, (
        "gatus-sync.timer must sit OUTSIDE the conditional half of the "
        f"expression, got: {expr!r}"
    )
