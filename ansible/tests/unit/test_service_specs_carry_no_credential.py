"""Keycloak's credentials and the shared admin password are absent from the
service specs `docker service inspect` shows (CV7).

Keycloak reads its DB password and its initial admin's password from a config
file mounted as a swarm secret (KC_CONFIG_FILE). The Beszel hub is given no
credential at all: the seed claims a hub with no user through its first-user
endpoint (scripts/beszel-seed.py claim_first_run).

The templates go through ansible-core's own templating.

Run: uv run pytest tests/unit/test_service_specs_carry_no_credential.py
"""
from __future__ import annotations

from pathlib import Path

import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.plugins.loader import add_all_plugin_dirs
from ansible.template import Templar, trust_as_template

ANSIBLE = Path(__file__).resolve().parents[2]
ROLES = ANSIBLE / "reconcile" / "roles"
KEYCLOAK = ROLES / "keycloak"
INFRA = ROLES / "infrastructure"

add_all_plugin_dirs(str(ANSIBLE / "playbooks"))

DB_PASSWORD = "k3+db/pass=word=="
ADMIN_PASSWORD = "Adm1n-pass_word"


def _render(text: str, variables: dict):
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(text))


def _tasks(path: Path) -> list[dict]:
    return [t for t in yaml.safe_load(path.read_text()) if isinstance(t, dict)]


def _index(tasks: list[dict], name: str) -> int:
    return next(i for i, t in enumerate(tasks) if name in str(t.get("name", "")))


def _keycloak_service() -> tuple[dict, dict]:
    defaults = yaml.safe_load((KEYCLOAK / "defaults" / "main.yml").read_text())
    doc = yaml.safe_load(_render((KEYCLOAK / "templates" / "keycloak.compose.yml.j2").read_text(), {
        "ansible_managed": "managed",
        "keycloak_server_service": "server",
        "keycloak_image": "quay.io/phasetwo/phasetwo-keycloak:test",
        "keycloak_db_name": "keycloak",
        "keycloak_db_user": "keycloak",
        "keycloak_db_password": DB_PASSWORD,
        "keycloak_config_target": defaults["keycloak_config_target"],
        "_keycloak_secrets": [{"name": "keycloak-conf-0123abcd"}],
        "keycloak_http_enabled": True,
        "catena_secondary_zones": [],
        "keycloak_hostname": "auth.acme.test",
        "keycloak_proxy_headers": "xforwarded",
        "admin_email": "admin@acme.test",
        "admin_password": ADMIN_PASSWORD,
        "keycloak_server_alias": "keycloak-server",
        "keycloak_management_port": 9000,
        "keycloak_health_path": "/health/ready",
    }))
    return doc, defaults


def test_keycloaks_spec_carries_neither_password():
    doc, defaults = _keycloak_service()
    server = doc["services"]["server"]
    env = server["environment"]
    assert "KC_DB_PASSWORD" not in env and "KC_BOOTSTRAP_ADMIN_PASSWORD" not in env
    assert not {DB_PASSWORD, ADMIN_PASSWORD} & {str(v) for v in env.values()}
    target = defaults["keycloak_config_target"]
    assert env["KC_CONFIG_FILE"] == f"/run/secrets/{target}"
    assert server["secrets"] == [{"source": "keycloak-conf-0123abcd", "target": target}]
    assert doc["secrets"] == {"keycloak-conf-0123abcd": {"external": True}}


def test_keycloaks_config_file_holds_both_passwords_as_properties():
    """java.util.Properties: key=value, the value read verbatim to the end of
    the line for these alphabets (base64 and URL-safe base64)."""
    rendered = _render((KEYCLOAK / "templates" / "keycloak.conf.j2").read_text(), {
        "keycloak_db_password": DB_PASSWORD, "admin_password": ADMIN_PASSWORD})
    props = dict(line.split("=", 1) for line in rendered.splitlines() if line.strip())
    assert props == {"db-password": DB_PASSWORD, "bootstrap-admin-password": ADMIN_PASSWORD}


def test_keycloaks_secret_exists_before_its_stack_deploys():
    tasks = _tasks(KEYCLOAK / "tasks" / "deploy.yml")
    name = _index(tasks, "name the credentials file's swarm secret")
    create = _index(tasks, "create the missing swarm secret")
    deploy = _index(tasks, "deploy as a swarm stack")
    assert name < create < deploy
    assert tasks[name].get("no_log") is True
    assert "keycloak.conf.j2" in str(tasks[name]["ansible.builtin.set_fact"]["_keycloak_secrets"])


def test_the_beszel_hub_spec_carries_no_credential():
    doc = yaml.safe_load(_render((INFRA / "templates" / "beszel-hub.compose.yml.j2").read_text(), {
        "ansible_managed": "managed",
        "beszel_image": "henrygd/beszel:test",
        "admin_email": "admin@acme.test",
        "admin_password": ADMIN_PASSWORD,
        "catena_public_surface_deferred": False,
        "beszel_hostname": "beszel.acme.test",
        "beszel_hub_network_alias": "beszel-hub",
        "beszel_hub_internal_port": 8090,
        "beszel_hub_data_volume": "beszel-hub-data",
        "beszel_hub_host_localhost_port": 18190,
        "beszel_network": "catena-beszel",
    }))
    env = doc["services"]["app"]["environment"]
    assert not {"USER_EMAIL", "USER_PASSWORD"} & set(env)
    assert ADMIN_PASSWORD not in yaml.safe_dump(doc)
