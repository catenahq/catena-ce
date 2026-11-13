"""reconcile/roles/public_ports/tasks/validate.yml checks the fragment dir (only
root declares a port), the docker drop-in, and, when the payload is expected,
the reconciler unit and its timer, through the shared payload predicate.

Run: uv run pytest tests/unit/test_public_ports_validate.py
"""
from __future__ import annotations

import yaml

from ansible_jinja import b64, env, holds
from ansible_tree import role_dir

TASKS = yaml.safe_load((role_dir("public_ports") / "tasks" / "validate.yml").read_text())
ASSERT = "ansible.builtin.assert"


def _task(fragment: str) -> dict:
    for t in TASKS:
        if fragment in (t.get("name") or ""):
            return t
    raise AssertionError(f"no task whose name contains {fragment!r}")


def _when(task: dict, **ctx: object) -> bool:
    whens = task["when"] if isinstance(task["when"], list) else [task["when"]]
    return all(env().compile_expression(w)(**ctx) for w in whens)


def test_the_fragment_dir_is_root_only_and_the_dropin_is_root_0644():
    paths = _task("the fragment dir and the docker drop-in")["vars"]["_assert_paths"]
    by_path = {p["path"]: p for p in paths}
    frag = by_path["{{ public_ports_fragment_dir }}"]
    assert (frag["type"], frag["owner"], frag["group"], frag["mode"]) == (
        "directory", "root", "root", "0755")
    drop = by_path["/etc/systemd/system/docker.service.d/30-catena-public-ports.conf"]
    assert (drop["type"], drop["owner"], drop["group"], drop["mode"]) == (
        "file", "root", "root", "0644")


def test_the_dropin_must_start_the_reconciler():
    task = _task("docker start re-applies the rules")
    start = "ExecStartPost=-/usr/bin/systemd-run --no-block /usr/bin/systemctl start catena-public-ports.service\n"
    assert holds(task, ASSERT, _pp_val_dropin={"content": b64(start)})
    assert not holds(task, ASSERT, _pp_val_dropin={"content": b64("[Service]\n")})


def test_the_unit_is_asked_of_the_shared_predicate():
    inc = _task("is the reconciler expected here")["ansible.builtin.include_role"]
    assert inc["tasks_from"] == "_payload_expected"
    assert _task("is the reconciler expected here")["vars"]["_payload_paths"] == [
        "/etc/systemd/system/catena-public-ports.service"]


def test_an_expected_but_missing_unit_fails_naming_it():
    fail = _task("the reconciler is missing")
    assert "catena-public-ports.service" in fail["ansible.builtin.fail"]["msg"]
    assert _when(fail, _pp_val_expected=True, _pp_val_present=False)
    assert not _when(fail, _pp_val_expected=True, _pp_val_present=True)
    assert not _when(fail, _pp_val_expected=False, _pp_val_present=False)


def test_a_missing_unit_that_is_not_expected_is_deferred_out_loud():
    debug = _task("timer checks deferred")
    assert _when(debug, _pp_val_expected=False, _pp_val_present=False)
    assert not _when(debug, _pp_val_expected=True, _pp_val_present=False)


def test_the_timer_is_probed_only_when_the_unit_is_present():
    probe = _task("the timer's state")
    assert probe["loop"] == ["is-enabled", "is-active"]
    assert probe["when"] == "_pp_val_present | bool"


def _timer(verb: str, answer: str) -> bool:
    return holds(_task("timer is enabled and active"), ASSERT,
                 item={"item": verb, "stdout": answer + "\n"})


def test_the_timer_must_answer_enabled_and_active():
    assert _timer("is-enabled", "enabled")
    assert _timer("is-active", "active")
    assert not _timer("is-enabled", "disabled")
    assert not _timer("is-active", "inactive")
    assert not _timer("is-active", "enabled")
