"""A rotated credential leaves no swarm secret holding its old value: each
service that mounts content-named secrets (the panel, Healthchecks, the admin
tools' gates, Keycloak) removes, right after it is deployed, every secret of
its bases that no service mounts (CV7).

Run: uv run pytest tests/unit/test_rotated_swarm_secrets_are_removed.py
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
ROLES = ANSIBLE / "reconcile" / "roles"
PRUNE = ANSIBLE / "playbooks" / "tasks" / "swarm_secrets_prune.yml"

_spec = importlib.util.spec_from_file_location(
    "catena_admin_service", ANSIBLE / "playbooks" / "filter_plugins" / "catena_admin_service.py")
svc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(svc)


def _service(*names):
    return {"Spec": {"TaskTemplate": {"ContainerSpec": {
        "Secrets": [{"SecretName": n, "File": {"Name": "x"}} for n in names]}}}}


def test_only_unmounted_secrets_of_the_given_bases_are_picked():
    old = svc.secret_name("healthchecks_secret_key", "old")
    new = svc.secret_name("healthchecks_secret_key", "new")
    mail = svc.secret_name("healthchecks_email_password", "pw")
    names = [old, new, mail,
             svc.secret_name("catena_admin_gate_secret", "x"),
             svc.secret_name("oauth2-proxy-clients", "y"),
             "healthchecks_secret_key-notahash",
             "healthchecks_secret_key-0123abcd-extra",
             "catena_postgres_password"]
    picked = svc.secrets_unmounted(
        names, [_service(new), _service()],
        ["healthchecks_secret_key", "healthchecks_email_password"])
    assert picked == sorted([old, mail])


def test_a_base_sharing_a_prefix_with_another_is_matched_whole():
    names = [svc.secret_name("oauth2-proxy-catena-admin", "a"),
             svc.secret_name("oauth2-proxy-clients", "b")]
    assert svc.secrets_unmounted(names, [], ["oauth2-proxy-catena"]) == []
    assert svc.secrets_unmounted(names, [], ["oauth2-proxy-catena-admin"]) == names[:1]


def test_no_base_picks_nothing():
    assert svc.secrets_unmounted([svc.secret_name("b", "v")], [], []) == []


def _config_service(*names):
    return {"Spec": {"TaskTemplate": {"ContainerSpec": {
        "Configs": [{"ConfigName": n, "File": {"Name": "x"}} for n in names]}}}}


def test_the_kind_config_reads_the_configs_a_service_mounts():
    old = svc.secret_name("keycloak-providers", "old")
    new = svc.secret_name("keycloak-providers", "new")
    other = svc.secret_name("other-config", "x")
    services = [_config_service(new), _service(old)]
    assert svc.secrets_unmounted([old, new, other], services, ["keycloak-providers"],
                                 "config") == [old]


def test_the_kind_secret_ignores_a_config_of_the_same_name():
    name = svc.secret_name("keycloak-providers", "v")
    assert svc.secrets_unmounted([name], [_config_service(name)], ["keycloak-providers"]) == [name]
    assert svc.secrets_unmounted([name], [_service(name)], ["keycloak-providers"], "config") == [name]


def test_the_prune_runs_no_shell_and_removes_what_the_filter_picks():
    tasks = yaml.safe_load(PRUNE.read_text())
    assert all("ansible.builtin.command" in t for t in tasks), "a task runs through a shell"
    kind = "{{ swarm_secret_kind | default('secret') }}"
    assert tasks[0]["ansible.builtin.command"]["argv"][:3] == ["docker", kind, "ls"]
    remove = tasks[-1]
    assert remove["ansible.builtin.command"]["argv"] == ["docker", kind, "rm", "{{ item }}"]
    assert "swarm_secrets_unmounted" in remove["loop"] and "swarm_secret_bases" in remove["loop"]
    assert "swarm_secret_kind | default('secret')" in remove["loop"]


def _tasks(path: Path) -> list[dict]:
    return [t for t in yaml.safe_load(path.read_text()) if isinstance(t, dict)]


def _index(tasks, fragment):
    return next(i for i, t in enumerate(tasks) if fragment in str(t.get("name", "")))


def _prune(tasks):
    return next(i for i, t in enumerate(tasks)
                if str(t.get("ansible.builtin.include_tasks", "")).endswith(
                    "/tasks/swarm_secrets_prune.yml"))


def test_each_creator_prunes_its_own_bases_after_its_deploy():
    cases = (
        (ROLES / "catena-admin" / "tasks" / "deploy.yml", "reconcile has to have settled",
         "{{ catena_admin_secret_specs | map(attribute='base') | list }}"),
        (ROLES / "keycloak" / "tasks" / "deploy.yml", "deploy as a swarm stack",
         ["{{ keycloak_config_secret_base }}"]),
        (ROLES / "oauth2_proxy" / "tasks" / "deploy.yml", "deploy per-app instance stack",
         "{{ oauth2_proxy_apps | map(attribute='slug') | map('regex_replace', '^', "
         "'oauth2-proxy-') | list }}"),
    )
    for path, deploy, bases in cases:
        tasks = _tasks(path)
        prune = _prune(tasks)
        assert _index(tasks, deploy) < prune, path
        actual = tasks[prune]["vars"]["swarm_secret_bases"]
        if isinstance(actual, str):
            actual = " ".join(actual.split())
        assert actual == bases, path


def test_keycloak_prunes_the_configs_of_its_extension_after_its_deploy():
    tasks = _tasks(ROLES / "keycloak" / "tasks" / "deploy.yml")
    prunes = [t for t in tasks if str(t.get("ansible.builtin.include_tasks", "")).endswith(
        "/tasks/swarm_secrets_prune.yml")]
    assert len(prunes) == 2
    assert prunes[1]["vars"] == {"swarm_secret_kind": "config",
                                 "swarm_secret_bases": ["{{ keycloak_providers_config_base }}"]}
    assert _index(tasks, "deploy as a swarm stack") < tasks.index(prunes[1])


def test_the_oauth2_proxy_bases_are_the_ones_its_secrets_are_named_after():
    deploy = (ROLES / "oauth2_proxy" / "tasks" / "deploy.yml").read_text()
    assert "'base': 'oauth2-proxy-' ~ _app.slug" in deploy
