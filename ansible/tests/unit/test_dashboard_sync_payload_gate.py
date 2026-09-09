"""The dashboard-sync wiring depends on a file this repo no longer ships.

catena-dashboard-sync and its lib modules moved into the catena-admin payload,
so reconcile/roles/infrastructure now wires a timer around a binary reconcile/roles/payload put on
the host five roles earlier. Whether its absence is a defect depends on whether
THIS converge was the one that had to install it -- the same two-legged shape
reconcile/roles/cloudflare_tunnel and reconcile/roles/backup already use.

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
    / "ansible" / "reconcile" / "roles" / "infrastructure"
)
TASKS = _ROLE / "tasks" / "dashboard_sync.yml"
VALIDATE = _ROLE / "tasks" / "validate.yml"

_EXPECTED = "catena_payload_expected"
_MISSING = "catena_payload_missing"
_STAT = "_dashboard_sync_present"
_V_EXPECTED = "_infra_payload_expected"
_SHARED = "_payload_expected"

# One list, declared in the role's defaults, read by both callers.
_RECONCILER_PATHS = "{{ dashboard_sync_required_paths }}"

# The modules catena-dashboard-sync imports at module scope. Run 1216
# nc_s3_hot_recovery restored the binary without them -- /usr/local/bin is in
# backup_paths, the modules were not -- the gate read the binary as present, and
# the converge died inside systemd on ModuleNotFoundError with an empty journal.
_REQUIRED_MODULES = (
    "clients_provisioner.py",
    "gate_routes.py",
    "keycloak_client.py",
    "portainer_api.py",
)


def _tasks() -> list[dict]:
    return yaml.safe_load(TASKS.read_text())


def _validate_tasks() -> list[dict]:
    return yaml.safe_load(VALIDATE.read_text())


def _flatten(tasks: list, inherited: str = "") -> list[tuple[dict, str]]:
    """Every task with the condition it actually runs under.

    A task inside a `block` is gated by the block's `when` as well as its own,
    so a walk that only reads top-level tasks would report an inner task as
    ungated when it is not.
    """
    out: list[tuple[dict, str]] = []
    for task in tasks or []:
        combined = " ".join(x for x in (inherited, _cond(task)) if x)
        out.append((task, combined))
        for key in ("block", "rescue", "always"):
            if key in task:
                out.extend(_flatten(task[key], combined))
    return out


def _find(name_fragment: str) -> dict:
    for task, _ in _flatten(_tasks()):
        if name_fragment in (task.get("name") or ""):
            return task
    raise AssertionError(f"task not found: {name_fragment!r}")


def _gate(name_fragment: str) -> str:
    """The full condition a task runs under, enclosing blocks included."""
    for task, combined in _flatten(_tasks()):
        if name_fragment in (task.get("name") or ""):
            return combined
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


def test_the_decision_comes_from_the_shared_predicate():
    """Five sites asked this and two answered it differently. One include, one
    answer -- bootstrap/roles/common/tasks/_payload_expected.yml."""
    task = _find("is the reconciler expected on this host")
    assert task["ansible.builtin.include_role"]["tasks_from"] == _SHARED
    assert task["vars"]["_payload_paths"] == _RECONCILER_PATHS


def test_the_gate_names_the_modules_not_the_directory():
    """bootstrap/roles/common creates the lib dir at role position 1 for its own
    public-ports modules, so it exists on hosts the payload has never touched.
    Stat-ing the directory answers yes for another owner's files, which is how
    a half-restored host passed the gate on the run that added it."""
    defaults = yaml.safe_load(
        (_ROLE / "defaults" / "main.yml").read_text()
    )
    paths = defaults["dashboard_sync_required_paths"]
    assert paths[0] == "{{ dashboard_sync_script_path }}"
    for module in _REQUIRED_MODULES:
        assert any(p.endswith("/" + module) for p in paths), (
            f"{module} is imported at module scope by catena-dashboard-sync "
            f"and is not in dashboard_sync_required_paths"
        )
    assert "{{ dashboard_sync_lib_dir }}" not in paths, (
        "the bare lib dir is not evidence the payload landed -- bootstrap/roles/common "
        "creates it for its own modules"
    )


def test_a_missing_reconciler_fails_a_converge_that_installs_it():
    """Production. reconcile/roles/payload ran, the file should be there, and a timer
    firing at a path that does not exist shows up as apps silently losing their
    routes rather than as a failed converge."""
    task = _find("missing from a converge that installs it")
    assert "ansible.builtin.fail" in task
    cond = _cond(task)
    assert _EXPECTED in cond
    assert _MISSING in cond


def test_a_host_with_out_of_band_engines_defers_instead():
    """The bench, and any host where something else owns /usr/local/bin/catena-*.
    The converge asserting on another manager's property is the two-managers
    mistake in miniature."""
    task = _find("staged out of band")
    assert "ansible.builtin.debug" in task
    cond = _cond(task)
    assert f"not ({_EXPECTED}" in cond
    assert _MISSING in cond


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
    # The timer FILE is not in this list any more: both halves of the lane ship
    # in the payload now, so the converge no longer renders one. What is left is
    # everything that TOUCHES systemd, which is the property that mattered --
    # the file's presence never was the risk, starting a unit whose service is
    # absent was.
    runs_it = [
        "enable + start timer",
        "run once now",
    ]
    for fragment in runs_it:
        cond = _gate(fragment)
        assert _STAT in cond, (
            f"{fragment!r} is not gated on the reconciler existing; on a host "
            f"whose engines are staged out of band this is what fails the "
            f"converge the deferral above exists to avoid"
        )


def test_a_failed_reconciler_reports_its_own_log_and_still_fails():
    """systemd returns an exit code and a pointer to a journal that, on a DR
    host, is destroyed with the machine -- run 1216 nc_s3_hot_recovery failed
    here and left nothing readable anywhere. The rescue exists to capture the
    output, so it has to re-raise: a rescue that only logs turns a failed
    reconciler into a passing converge, which is worse than the silence."""
    names = [t.get("name") or "" for t, _ in _flatten(_tasks())]
    assert any("capture the reconciler's log" in n for n in names)

    fail_task = _find("fail with the reconciler's own output")
    assert "ansible.builtin.fail" in fail_task
    msg = str(fail_task["ansible.builtin.fail"]["msg"])
    assert "_dashboard_sync_journal" in msg, (
        "the failure must carry the reconciler's output, otherwise the rescue "
        "captured it and threw it away"
    )

    diag = _find("capture the reconciler's log")
    assert diag.get("failed_when") is False, (
        "an unavailable journal must not replace the real failure with a "
        "failure about reading the journal"
    )


def test_the_env_file_is_written_unconditionally():
    """It is the one artifact here whose correct value changes with the HOST,
    not with the payload -- the Portainer key, the oauth2-proxy client secret,
    the per-zone cookie secrets. Withholding it until the payload lands would
    make the next converge's first act a secret rotation."""
    task = _find("env file")
    assert _cond(task) == ""
    assert task["no_log"] is True


def test_the_converge_renders_no_timer_for_this_lane():
    """Both halves of the lane ship in the payload.

    Its service was always the payload's; its timer was rendered here from a
    template whose only variables were compiled-in constants -- so one lane had
    two owners and two release cadences, and a change to the pair was
    half-applied until an image AND a converge had landed, in that order.

    The general form is tests/unit/test_no_lane_is_half_owned.py. This is the
    specific one, because this lane is the reason that gate exists.
    """
    body = TASKS.read_text()
    assert "catena-dashboard-sync.timer.j2" not in body, (
        "the converge renders this timer again, which puts the lane back under "
        "two owners"
    )
    written = [
        str((task.get("ansible.builtin.template") or {}).get("dest", ""))
        for task, _ in _flatten(_tasks())
    ]
    assert not [d for d in written if d.endswith(".timer")], (
        f"a task writes a .timer for a lane whose units ship in the image: "
        f"{[d for d in written if d.endswith('.timer')]}"
    )


# --- validate.yml: the same property, the surface the first fix missed ------

def test_validate_uses_the_same_shared_predicate():
    """A standalone play cannot see reconcile/roles/payload's set_fact, and the shared
    predicate is what falls back to the env var -- so validate gets the
    fallback for free instead of hand-rolling it a second time."""
    task = _find_v("is the payload expected here")
    assert task["ansible.builtin.include_role"]["tasks_from"] == _SHARED
    assert task["vars"]["_payload_paths"] == _RECONCILER_PATHS


def test_validate_pins_the_decision_before_anything_overwrites_it():
    """The predicate sets play-level facts, so a later include from another
    role replaces them. Every caller reads them once, immediately."""
    pinned = _find_v("pin the payload decision")["ansible.builtin.set_fact"]
    assert _EXPECTED in str(pinned[_V_EXPECTED])


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
