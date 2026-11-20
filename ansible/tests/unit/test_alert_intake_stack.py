"""The alert intake is a swarm stack of its own, `catena-alerts`, that names only
its overlay, takes its three tokens as swarm secrets and publishes one loopback
port.

The properties held here:

- the rendered stack file names the overlay `alert_network` and no other
  network, publishes the loopback port in host mode, runs the binary as
  `nonroot` with its own health check and mounts each token where the binary
  reads it;
- the contract (contracts default-apps/wiring.json) puts the apps that post to
  it on that overlay, each listing its own source's token;
- the three tokens are minted store secrets, one source token per entry of the
  contract's `alert_intake_sources`;
- the port's fragment is loopback-only on the DNAT bind, and validate refuses a
  request with no token;
- the alert switches of the two sources are declared.

Run: uv run pytest tests/unit/test_alert_intake_stack.py
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import jinja2
import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
INFRA = ANSIBLE / "reconcile" / "roles" / "infrastructure"
GROUP_VARS = ANSIBLE / "playbooks" / "group_vars" / "all" / "main.yml"
WIRING = json.loads((ANSIBLE.parents[1] / "contracts" / "default-apps" / "wiring.json").read_text())

TOKENS = ("alert_intake_token_status", "alert_intake_token_resources", "alert_intake_token_read")


def _defaults() -> dict:
    return yaml.safe_load((INFRA / "defaults" / "main.yml").read_text())


def _group_vars() -> dict:
    return yaml.safe_load(GROUP_VARS.read_text())


def _tasks(name: str) -> list[dict]:
    return yaml.safe_load((INFRA / "tasks" / name).read_text())


def _filter(name: str):
    spec = importlib.util.spec_from_file_location(
        name, ANSIBLE / "playbooks" / "filter_plugins" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _render() -> dict:
    defaults, gv = _defaults(), _group_vars()
    env = jinja2.Environment(undefined=jinja2.StrictUndefined,
                             loader=jinja2.FileSystemLoader(INFRA / "templates"))
    specs = [{**s, "value": f"value-of-{s['target']}"} for s in defaults["alert_intake_secret_specs"]]
    secrets = _filter("catena_admin_service").secret_entries(specs)
    text = env.get_template("alert-intake.compose.yml.j2").render(
        ansible_managed="", catena_admin_image="registry.example/panel@sha256:0",
        alert_network=gv["alert_network"], _alert_intake_secrets=secrets,
        **{k: v for k, v in defaults.items() if k.startswith("alert_intake_")})
    doc = yaml.safe_load(text)
    doc["_secrets"] = secrets
    return doc


def test_the_stack_names_only_the_alert_overlay():
    doc = _render()
    network = _group_vars()["alert_network"]
    assert network == "catena-alerts"
    assert doc["networks"] == {network: {"external": True}}
    [service] = doc["services"].values()
    assert set(service["networks"]) == {network}
    assert service["networks"][network]["aliases"] == ["catena-alert-intake"]
    assert "catena-network" not in json.dumps(doc)


def test_the_service_runs_the_intake_binary_as_nonroot_on_the_panels_image():
    [service] = _render()["services"].values()
    assert service["image"] == "registry.example/panel@sha256:0"
    assert service["entrypoint"] == ["/usr/local/bin/catena-alert-intake"]
    assert service["user"] == "nonroot"
    assert service["healthcheck"]["test"] == ["CMD", "/usr/local/bin/catena-alert-intake", "health"]
    assert "disable" not in service["healthcheck"]


def test_the_port_is_a_host_mode_publish_and_the_state_a_named_volume():
    doc = _render()
    [service] = doc["services"].values()
    assert service["ports"] == [
        {"target": 8080, "published": 18070, "protocol": "tcp", "mode": "host"}]
    assert service["volumes"] == ["intake-state:/var/lib/catena-alert-intake"]
    assert doc["volumes"] == {"intake-state": {}}


def test_each_token_is_a_content_named_swarm_secret_mounted_by_its_store_key():
    doc = _render()
    [service] = doc["services"].values()
    mounted = {s["target"]: s["source"] for s in service["secrets"]}
    assert set(mounted) == set(TOKENS)
    for target, source in mounted.items():
        assert source.startswith(target.replace("_", "-") + "-"), source
        assert doc["secrets"][source] == {"external": True}
    assert "value-of" not in json.dumps({k: v for k, v in doc.items() if k != "_secrets"})


def test_the_deploy_creates_the_secrets_then_the_stack_and_prunes_the_rest():
    tasks = _tasks("alert_intake.yml")
    names = [t["name"] for t in tasks]
    assert names.index("Alert intake: create the missing swarm secrets") < \
        names.index("Alert intake: deploy as a swarm stack")
    deploy = next(t for t in tasks if t["name"] == "Alert intake: deploy as a swarm stack")
    assert deploy["vars"]["svc_name"] == "{{ alert_intake_compose_name }}"
    assert deploy["vars"]["svc_domain_host"] == ""
    named = next(t for t in tasks if "name each token" in t["name"])
    assert named["no_log"] is True
    assert _defaults()["alert_intake_compose_name"] == "catena-alerts"


def test_the_intake_is_deployed_before_the_apps_that_post_to_it():
    names = [t["name"] for t in _tasks("main.yml")]
    intake = next(i for i, n in enumerate(names) if n.startswith("Alert intake"))
    assert intake < next(i for i, n in enumerate(names) if n.startswith("Default apps"))


def test_the_fragment_declares_the_port_loopback_only_on_the_dnat_bind():
    declare = next(t for t in _tasks("alert_intake.yml") if "declare the host-side port" in t["name"])
    body = declare["ansible.builtin.copy"]["content"]
    assert '"scope": "loopback", "bind": "docker"' in body
    assert '"bind": "host"' not in body
    assert '"port": alert_intake_host_localhost_port | int' in body
    assert declare["ansible.builtin.copy"]["dest"] == "{{ public_ports_fragment_dir }}/{{ alert_intake_compose_name }}.json"
    start = next(t for t in _tasks("alert_intake.yml") if "reconcile public ports now" in t["name"])
    assert "_alert_intake_ports_fragment.changed" in start["when"]
    assert "catena_public_ports_installed | default(true) | bool" in start["when"]


def test_validate_checks_health_and_refuses_a_request_with_no_token():
    uris = {t["name"]: t["ansible.builtin.uri"] for t in _tasks("validate.yml")
            if "ansible.builtin.uri" in t}
    health = uris["validate/infrastructure: the alert intake answers /health on the loopback"]
    assert health["url"] == "http://127.0.0.1:{{ alert_intake_host_localhost_port }}/health"
    assert health["status_code"] == 200
    refused = uris["validate/infrastructure: the alert intake refuses a request with no token"]
    assert refused["url"].endswith("/v1/state")
    assert "Authorization" not in json.dumps(refused)
    assert refused["status_code"] == 401


def test_the_three_tokens_are_minted_store_secrets_and_the_specs_name_them():
    spec = importlib.util.spec_from_file_location(
        "onbox_config", ANSIBLE / "helpers" / "onbox_config.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for token in TOKENS:
        assert mod.INTERNAL_SECRETS[token] is mod.mint_url_safe, token
    assert [s["target"] for s in _defaults()["alert_intake_secret_specs"]] == list(TOKENS)


def test_a_token_is_named_for_its_source_not_for_a_monitor():
    sources = WIRING["alert_intake_sources"]
    assert {f"alert_intake_token_{source}" for source in sources} <= set(TOKENS)
    for app in sources.values():
        assert app in WIRING["apps"]


def test_the_apps_that_post_are_on_the_alert_overlay_with_their_own_token():
    gv = _group_vars()
    for source, app in WIRING["alert_intake_sources"].items():
        entry = WIRING["apps"][app]
        assert f"alert_intake_token_{source}" in entry["secrets"], app
        on_alerts = [name for name, svc in entry["services"].items()
                     if gv["alert_network"] in (svc.get("networks") or {})]
        assert on_alerts, app


def test_the_alert_switches_of_the_two_sources_are_declared_off_by_default():
    knobs = {k["key"]: k for k in yaml.safe_load((ANSIBLE / "helpers" / "knobs.yml").read_text())["config"]}
    for base in ("ALERT_SERVICE_DOWN", "ALERT_RESOURCES"):
        for suffix in ("", "_CLEAR", "_REMIND"):
            knob = knobs[base + suffix]
            assert knob["residence"] == "store", knob
            assert "var" not in knob and "label" not in knob and "help" not in knob, knob
            panel = knob["panel"]
            assert panel["group"] == "alerts" and panel["kind"] == "choice", knob
            assert panel["options"] == ["off", "on"] and "default" not in panel, knob
