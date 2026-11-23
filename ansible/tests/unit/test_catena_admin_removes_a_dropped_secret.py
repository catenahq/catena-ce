"""A base that leaves catena_admin_secret_specs leaves no swarm secret behind.

The converge's prune also removes every unmounted secret of the panel's
prefix, so a base the specs dropped is removed, a declared one a service
mounts is not, and a secret of another prefix is not touched. validate fails
on a panel secret no declared base accounts for.

Run: uv run pytest tests/unit/test_catena_admin_removes_a_dropped_secret.py
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import jinja2
import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
ROLE = ANSIBLE / "reconcile" / "roles" / "catena-admin"
PREFIX = "catena_admin_"

_spec = importlib.util.spec_from_file_location(
    "catena_admin_service", ANSIBLE / "playbooks" / "filter_plugins" / "catena_admin_service.py")
svc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(svc)


def _service(*names):
    return {"Spec": {"TaskTemplate": {"ContainerSpec": {
        "Secrets": [{"SecretName": n, "File": {"Name": "x"}} for n in names]}}}}


def _tasks(path: Path) -> list[dict]:
    return [t for t in yaml.safe_load(path.read_text()) if isinstance(t, dict)]


def test_a_dropped_base_is_listed_for_removal():
    dropped = svc.secret_name("catena_admin_old_key", "v")
    assert svc.secrets_unmounted([dropped], [_service()], ["catena_admin_gate_secret"],
                                 prefix=PREFIX) == [dropped]


def test_a_declared_base_is_not_listed_while_a_service_mounts_it():
    gate = svc.secret_name("catena_admin_gate_secret", "v")
    assert svc.secrets_unmounted([gate], [_service(gate)], ["catena_admin_gate_secret"],
                                 prefix=PREFIX) == []


def test_a_dropped_base_a_service_mounts_is_kept():
    dropped = svc.secret_name("catena_admin_old_key", "v")
    assert svc.secrets_unmounted([dropped], [_service(dropped)], [], prefix=PREFIX) == []


def test_a_secret_of_another_prefix_is_not_listed():
    names = [svc.secret_name("oauth2-proxy-status", "v"),
             svc.secret_name("catena_adminx", "v"),
             "catena_admin_old_key",
             "catena_admin_old_key-nothash"]
    assert svc.secrets_unmounted(names, [], ["catena_admin_gate_secret"], prefix=PREFIX) == []


def test_no_prefix_keeps_the_bases_only_behaviour():
    dropped = svc.secret_name("catena_admin_old_key", "v")
    assert svc.secrets_unmounted([dropped], [], ["catena_admin_gate_secret"]) == []


def test_the_panel_passes_its_prefix_to_the_prune():
    prune = next(t for t in _tasks(ROLE / "tasks" / "deploy.yml")
                 if str(t.get("ansible.builtin.include_tasks", "")).endswith(
                     "/tasks/swarm_secrets_prune.yml"))
    assert prune["vars"]["swarm_secret_prefix"] == PREFIX


def test_the_prune_hands_the_prefix_to_the_filter():
    prune = yaml.safe_load(
        (ANSIBLE / "playbooks" / "tasks" / "swarm_secrets_prune.yml").read_text())[-1]
    assert "swarm_secret_prefix | default('')" in prune["loop"]


def test_validate_fails_on_a_panel_secret_outside_the_declared_bases():
    check = next(t for t in _tasks(ROLE / "tasks" / "validate.yml")
                 if "no panel secret outside the declared ones" in t.get("name", ""))
    assert check["ansible.builtin.assert"]["that"] == "_ca_val_stray_secrets | length == 0"
    expr = " ".join(check["vars"]["_ca_val_stray_secrets"].split())
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.filters["swarm_secrets_outside_bases"] = svc.secrets_outside_bases
    render = lambda names: env.compile_expression(  # noqa: E731
        expr.removeprefix("{{").removesuffix("}}").strip())(
            _ca_val_secrets={"stdout_lines": names},
            catena_admin_secret_specs=[{"base": "catena_admin_gate_secret"}])
    gate = svc.secret_name("catena_admin_gate_secret", "v")
    old = svc.secret_name("catena_admin_old_key", "v")
    other = svc.secret_name("oauth2-proxy-status", "v")
    assert render([gate, other]) == []
    assert render([gate, old, other]) == [old]
