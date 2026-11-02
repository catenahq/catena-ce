"""Catena's own stacks that keep data declare it in an x-catena block, and the
swarm stack helper asks the host for the copy before it deploys.

Keycloak's schema migrates on the first start of a newer version and the older
one cannot read it. The block names the service and its database;
catena-stack-update-managed infra-data copy copies the data of each declared
service whose image the new stack file changes, and puts it back on a rollback
of the move.

Run: uv run pytest tests/unit/test_infra_data_copy.py
"""
from __future__ import annotations

import jinja2
import yaml

from ansible_tree import PLAYBOOKS, panel_vars, role_dir, walk_tasks

TEMPLATES = role_dir("infrastructure") / "templates"
STACK = role_dir("infrastructure") / "tasks" / "swarm_stack.yml"


class _Placeholder(jinja2.ChainableUndefined):
    """A variable the render is not given prints as a word, so the file stays
    valid YAML without the roles' defaults."""

    def __str__(self) -> str:
        return "x"


KEYCLOAK = role_dir("keycloak")


def _render(name: str, templates=TEMPLATES, **values) -> dict:
    env = jinja2.Environment(undefined=_Placeholder)
    env.filters["bool"] = bool
    text = (templates / name).read_text(encoding="utf-8")
    return yaml.safe_load(env.from_string(text).render(
        catena_public_surface_deferred=False,
        infrastructure_smtp={"enabled": False, "sender": "x", "implicit_tls": False},
        **values,
    ))


def _keycloak_names() -> tuple[str, str]:
    """The stack and the server service key, as the roles name them."""
    names = {}
    for path in (KEYCLOAK / "defaults" / "main.yml", KEYCLOAK / "vars" / "main.yml"):
        names.update(yaml.safe_load(path.read_text(encoding="utf-8")))
    return names["keycloak_compose_name"], names["keycloak_server_service"]


def _keycloak() -> dict:
    _, server = _keycloak_names()
    return _render("keycloak.compose.yml.j2", KEYCLOAK / "templates", keycloak_server_service=server)


def test_keycloak_declares_the_database_of_its_server():
    """Keycloak's schema migrates on the first start of a newer version and the
    older one cannot read it: its database, in catena-postgres, is copied
    before the image moves."""
    _, server = _keycloak_names()
    doc = _keycloak()
    assert doc["x-catena"] == {
        "schema": 2,
        "data": {server: {"database": "catena-postgres", "upgrade": "in_place"}},
    }
    assert server in doc["services"], "the block names a service the file lacks"
    assert [k for k in doc if k.startswith("x-")] == ["x-catena"]


def test_the_verb_sees_the_swarm_service_the_bump_manifest_names():
    """The converge asks about the stack named keycloak_compose_name and the
    service key of the block; swarm names the service <stack>_<service>, which
    is the swarm_service of Keycloak's entry in the managed services."""
    stack, server = _keycloak_names()
    manifest = (role_dir("catena-admin") / "templates" / "managed-services.json.j2").read_text(
        encoding="utf-8")
    assert '"swarm_service": "{{ keycloak_compose_name }}_{{ keycloak_server_service }}"' in manifest
    assert f"{stack}_{server}" == "keycloak_server"


def _stack_tasks() -> list[dict]:
    return walk_tasks(yaml.safe_load(STACK.read_text(encoding="utf-8")))


def _index(fragment: str) -> int:
    for i, task in enumerate(_stack_tasks()):
        if fragment in (task.get("name") or ""):
            return i
    raise AssertionError(f"task not found: {fragment!r}")


def test_the_copy_runs_between_the_stack_file_and_the_deploy():
    copy, write = _index("copy the data a moved image migrates"), _index("write the stack file")
    attempt = _index("deploy with retry on registry transient")
    assert write < copy < attempt


def test_the_copy_asks_the_verb_for_this_stack_and_file():
    task = _stack_tasks()[_index("copy the data a moved image migrates")]
    argv = task["ansible.builtin.command"]["argv"]
    assert argv == [
        "{{ catena_admin_stack_update_bin }}", "infra-data", "copy",
        "--stack", "{{ svc_name }}",
        "--file", "{{ swarm_stack_dir }}/{{ svc_name }}.yml",
        "--run", "{{ lookup('env', 'CATENA_INFRA_RUN') }}",
    ]
    assert panel_vars()["catena_admin_stack_update_bin"].endswith("/catena-stack-update-managed")


def test_a_refusal_fails_the_task_and_a_copy_reports_no_change():
    """No failed_when and no ignore_errors: a refused copy stops the converge
    before the deploy, so a stack is never deployed onto data with no copy."""
    task = _stack_tasks()[_index("copy the data a moved image migrates")]
    assert task["changed_when"] is False
    for key in ("failed_when", "ignore_errors", "when"):
        assert key not in task, f"{key} lets a stack deploy onto data with no copy"


def test_every_caller_of_the_helper_runs_after_the_payload_that_installs_the_verb():
    reconcile = yaml.safe_load((PLAYBOOKS / "reconcile.yml").read_text(encoding="utf-8"))[0]
    roles = [r["role"] if isinstance(r, dict) else r for r in reconcile["roles"]]
    for caller in ("keycloak", "oauth2_proxy", "infrastructure"):
        assert roles.index("payload") < roles.index(caller)
