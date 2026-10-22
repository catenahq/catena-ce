"""A Portainer left on a host is removed only once no client stack is left on it.

`catena-apps adopt` gives every client stack a folder and reports the stacks it
could not take. The portainer_exit_verdict filter turns that report into the
decision; the role removes Portainer, its data volume and what served it on a
yes, and on a no leaves it running and says why.

Run: uv run pytest tests/unit/test_portainer_exit.py
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import yaml

_ANSIBLE = Path(__file__).resolve().parents[2]
_ROLE = _ANSIBLE / "reconcile" / "roles" / "portainer_exit"

_spec = importlib.util.spec_from_file_location(
    "portainer_exit", _ANSIBLE / "playbooks" / "filter_plugins" / "portainer_exit.py")
pe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pe)


def _ran(report, rc=0, stderr=()):
    return {"rc": rc, "stdout": json.dumps(report), "stderr_lines": list(stderr)}


def _tasks() -> list[dict]:
    return yaml.safe_load((_ROLE / "tasks" / "main.yml").read_text())


def _named(fragment: str) -> dict:
    for task in _tasks():
        if fragment in task.get("name", ""):
            return task
    raise AssertionError(f"no task named ...{fragment}...")


def test_portainer_goes_when_the_pass_refused_no_stack():
    verdict = pe.portainer_exit_verdict(_ran({"adopted": ["web"], "kept": ["blog"], "refused": []}))
    assert verdict == {"remove": True, "staying": [], "why": ""}


def test_portainer_stays_and_names_the_stacks_the_pass_refused():
    verdict = pe.portainer_exit_verdict(_ran({
        "adopted": ["web"], "kept": [],
        "refused": [{"stack": "shop", "reason": "its settings cannot be written to a .env"},
                    {"stack": "Bad_Name", "reason": "not a stack name"}]}))
    assert verdict["remove"] is False
    assert verdict["staying"] == ["shop", "Bad_Name"]
    assert "shop: its settings cannot be written to a .env" in verdict["why"]
    assert "Bad_Name: not a stack name" in verdict["why"]


def test_portainer_stays_when_the_pass_failed_or_did_not_run():
    failed = pe.portainer_exit_verdict(_ran({}, rc=1, stderr=["catena-apps: adopt: connection refused"]))
    assert failed["remove"] is False and "connection refused" in failed["why"]
    assert pe.portainer_exit_verdict({"skipped": True})["remove"] is False
    assert pe.portainer_exit_verdict(None)["remove"] is False


def test_portainer_stays_on_a_report_it_cannot_read():
    for stdout in ("", "not json", "{}", '{"refused": [{"reason": "x"}]}', '{"refused": null}'):
        verdict = pe.portainer_exit_verdict({"rc": 0, "stdout": stdout})
        assert verdict["remove"] is False, stdout


def test_the_adoption_runs_inside_the_converges_hold_and_carries_no_secret():
    task = _named("adopt the client stacks as application folders")
    assert task["ansible.builtin.command"]["argv"] == ["/usr/local/bin/catena-apps", "adopt", "-held"]
    assert "environment" not in task
    assert task["no_log"] is True
    assert task["failed_when"] is False


def test_the_adoption_waits_for_a_portainer_and_an_installed_binary():
    task = _named("adopt the client stacks as application folders")
    assert "_pe_service.rc == 0" in task["when"]
    assert "_pe_apps_bin.stat.exists" in task["when"]


def test_a_portainer_that_stays_is_reported_and_does_not_fail_the_converge():
    said = _named("Portainer stays, and why")
    assert "_pe_verdict.why" in said["ansible.builtin.debug"]["msg"]
    assert "not _pe_verdict.remove" in said["when"]
    assert not [t for t in _tasks() if "ansible.builtin.fail" in t]


def test_the_removal_waits_for_the_verdict_and_follows_the_adoption():
    names = [t.get("name", "") for t in _tasks()]
    removal = _named("remove Portainer and what it was made of")
    assert removal["when"] == "_pe_service.rc != 0 or _pe_verdict.remove"
    assert names.index(removal["name"]) > names.index(_named("decide whether Portainer may go")["name"])
    inner = [t["name"] for t in removal["block"]]
    assert any("remove the service" in n for n in inner)
    assert any("remove the data volume" in n for n in inner)
    # No task outside that block removes the service or its volume.
    for task in _tasks():
        if task is removal:
            continue
        argv = (task.get("ansible.builtin.command") or {}).get("argv", [])
        assert not (argv[:3] in (["docker", "service", "rm"], ["docker", "volume", "rm"])), task["name"]


def test_the_gate_is_cleaned_up_whether_or_not_portainer_stays():
    route = _named("remove the route of Portainer's sign-in gate")
    assert "when" not in route and route["ansible.builtin.file"]["state"] == "absent"
    assert "when" not in _named("remove the swarm secrets of Portainer's sign-in gate")


def test_both_playbooks_run_the_role_after_the_sign_in_gates():
    for name in ("converge.yml", "reconcile.yml"):
        roles = [r["role"] for r in yaml.safe_load((_ANSIBLE / "playbooks" / name).read_text())[0]["roles"]]
        assert "portainer" not in roles
        assert roles.index("portainer_exit") == roles.index("oauth2_proxy") + 1, name


def test_no_converge_deploys_or_requires_a_portainer_service():
    required = yaml.safe_load((_ANSIBLE / "reconcile/roles/tier1_stack/defaults/main.yml").read_text())
    assert "catena-portainer" not in required["tier1_stack_required"]
    assert not (_ANSIBLE / "reconcile" / "roles" / "portainer").exists()
