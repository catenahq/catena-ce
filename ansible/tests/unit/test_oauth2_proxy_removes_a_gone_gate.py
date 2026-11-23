"""A gate whose entry leaves oauth2_proxy_apps leaves nothing behind.

The stack deploy's --prune removes the gate's instance. Its route file would
keep sending its host to that missing instance at priority 100, above any route
an application's own gate writes, and its swarm secret would keep the cookie
secret no service reads, so the deploy removes both, and validate fails on a
route file no entry declares.

Run: uv run pytest tests/unit/test_oauth2_proxy_removes_a_gone_gate.py
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import jinja2
import yaml

ROLE = Path(__file__).resolve().parents[2] / "reconcile" / "roles" / "oauth2_proxy"
DEPLOY = ROLE / "tasks" / "deploy.yml"
VALIDATE = ROLE / "tasks" / "validate.yml"
DIR = "/etc/catena/traefik/dynamic"
APPS = [{"slug": "gatus"}, {"slug": "catena-admin"}, {"slug": "beszel"}]


def _env() -> jinja2.Environment:
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.filters["basename"] = os.path.basename
    env.filters["regex_replace"] = lambda value, pattern, repl: re.sub(pattern, repl, value)
    return env


def _found(*names: str) -> dict:
    return {"files": [{"path": f"{DIR}/{n}"} for n in names]}


def _tasks(path: Path) -> list[dict]:
    return yaml.safe_load(path.read_text())


def _task(tasks: list[dict], fragment: str) -> dict:
    return next(t for t in tasks if fragment in t.get("name", ""))


def _gone(found: dict) -> list[str]:
    expr = _task(_tasks(DEPLOY), "the gates no entry declares")[
        "ansible.builtin.set_fact"]["_oauth2_gone_slugs"]
    return _env().compile_expression(expr.strip().removeprefix("{{").removesuffix("}}"))(
        _oauth2_route_files=found, oauth2_proxy_apps=APPS)


def test_the_route_files_are_listed_in_the_traefik_dir():
    find = _task(_tasks(DEPLOY), "find the gates' route files")["ansible.builtin.find"]
    assert find == {"paths": "{{ traefik_dynamic_dir }}", "patterns": "*-oauth2-proxy.yml"}


def test_only_a_gate_no_entry_declares_is_gone():
    found = _found("gatus-oauth2-proxy.yml", "catena-admin-oauth2-proxy.yml",
                   "beszel-oauth2-proxy.yml", "old-oauth2-proxy.yml")
    assert _gone(found) == ["old"]
    assert _gone(_found("gatus-oauth2-proxy.yml")) == []
    assert _gone(_found()) == []


def test_the_gone_gates_secret_is_pruned_with_the_declared_ones():
    prune = _task(_tasks(DEPLOY), "remove the swarm secrets no service mounts")
    bases = _env().from_string(prune["vars"]["swarm_secret_bases"]).render(
        oauth2_proxy_apps=APPS, _oauth2_gone_slugs=["old"]).strip()
    assert bases == str(["oauth2-proxy-gatus", "oauth2-proxy-catena-admin",
                         "oauth2-proxy-beszel", "oauth2-proxy-old"])


def test_the_route_file_goes_after_the_deploy_and_the_prune():
    """A converge that stops before the prune finds the route file again, and
    with it the secret still to remove."""
    tasks = _tasks(DEPLOY)
    names = [t.get("name", "") for t in tasks]
    remove = _task(tasks, "remove the route files")
    assert remove["ansible.builtin.file"] == {
        "path": "{{ traefik_dynamic_dir }}/{{ item }}-oauth2-proxy.yml", "state": "absent"}
    assert remove["loop"] == "{{ _oauth2_gone_slugs }}"
    order = [next(i for i, n in enumerate(names) if f in n)
             for f in ("deploy per-app instance stack", "remove the swarm secrets",
                       "remove the route files")]
    assert order == sorted(order)


def test_validate_fails_on_a_route_file_no_entry_declares():
    tasks = _tasks(VALIDATE)
    check = _task(tasks, "every route file belongs to a declared gate")
    stray = check["vars"]["_oauth2_stray_routes"]
    render = lambda found: _env().compile_expression(  # noqa: E731
        stray.strip().removeprefix("{{").removesuffix("}}"))(
            _oauth2_validate_routes=found, oauth2_proxy_apps=APPS)
    assert render(_found("gatus-oauth2-proxy.yml", "beszel-oauth2-proxy.yml")) == []
    assert render(_found("gatus-oauth2-proxy.yml", "old-oauth2-proxy.yml")) == [
        "old-oauth2-proxy.yml"]
    assert check["ansible.builtin.assert"]["that"] == "_oauth2_stray_routes | length == 0"
    listing = _task(tasks, "list the gates' route files")["ansible.builtin.find"]
    assert listing["patterns"] == "*-oauth2-proxy.yml"
