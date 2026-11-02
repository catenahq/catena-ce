"""Gatus, Healthchecks and Beszel are catalog apps that
reconcile/roles/infrastructure adds with `catena-apps defaults`.

The properties held here, against the wiring contract
(contracts default-apps/wiring.json):

- each app's stack is the contract's, and the SQLite copy list, the Gatus config
  folder, the loopback ports, the network aliases and the infra spec's stacks
  follow from it;
- `/etc/catena/default-apps.json` lists the apps, with no secret;
- the env map the converge writes is the contract's `env_core_writes` and holds
  no key the app's own x-catena block declares (the engine refuses one);
- the apps set themselves up: no seed, agent or alert shim task remains;
- the address label of each app is read from its folder before any role runs.

Run: uv run pytest tests/unit/test_default_apps.py
"""
from __future__ import annotations

import ast
import importlib.util
import json
import re
from pathlib import Path

import jinja2
import jinja2.nativetypes
import pytest
import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
REPO = ANSIBLE.parent
INFRA = ANSIBLE / "reconcile" / "roles" / "infrastructure"
TASKS = INFRA / "tasks"
PLAYBOOKS = ANSIBLE / "playbooks"
GROUP_VARS = PLAYBOOKS / "group_vars" / "all" / "main.yml"
TEMPLATES = REPO.parent / "catena-templates" / "blueprints"
WIRING = REPO.parent / "contracts" / "default-apps" / "wiring.json"
STORAGE = "/srv/catena"

CONTRACT = json.loads(WIRING.read_text())["apps"]
STACKS = {app: entry["stack"] for app, entry in CONTRACT.items()}

# What the apps' x-catena blocks declare: the settings (the address, the
# monitors' URLs, the title) and the contract's generated or managed values. The
# engine refuses a request that writes one of these in env.
SETTINGS = {"SUBDOMAIN", "HC_URL", "HUB_URL", "UI_TITLE"}
DECLARED = SETTINGS | {key for entry in CONTRACT.values() for key in entry["env_declared"]}


def _group_vars() -> dict:
    return yaml.safe_load(GROUP_VARS.read_text())


def _defaults() -> dict:
    return yaml.safe_load((INFRA / "defaults" / "main.yml").read_text())


def _tasks(name: str) -> list[dict]:
    return yaml.safe_load((TASKS / name).read_text())


def _fact(tasks: list[dict], key: str) -> dict:
    return next(t["ansible.builtin.set_fact"][key] for t in tasks
                if key in (t.get("ansible.builtin.set_fact") or {}))


def _env_maps() -> dict[str, dict]:
    """The values each converge writes on every run."""
    gatus = next(t["vars"]["default_app_env"] for t in _tasks("gatus.yml")
                 if "default_app.yml" in str(t.get("ansible.builtin.include_tasks")))
    return {
        "gatus": gatus,
        "healthchecks": _fact(_tasks("healthchecks.yml"), "_healthchecks_env"),
        "beszel": _fact(_tasks("beszel.yml"), "_beszel_hub_env"),
    }


def _block_declared(app: str) -> set[str]:
    """The env keys the checked-out blueprint's own x-catena block declares,
    none when the catena-templates checkout is absent."""
    block = TEMPLATES / app / "x-catena.yml"
    if not block.is_file():
        return set()
    return set((yaml.safe_load(block.read_text()) or {}).get("env") or {})


def _load_filter():
    spec = importlib.util.spec_from_file_location(
        "dotenv_value", PLAYBOOKS / "filter_plugins" / "dotenv_value.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.dotenv_value


# --- the list ---------------------------------------------------------------

def test_every_entry_is_the_contracts_stack():
    apps = {a["app"]: a for a in _group_vars()["default_apps"]}
    assert {app: a["stack"] for app, a in apps.items()} == STACKS
    for app, entry in apps.items():
        assert re.fullmatch(rf"{app}/\d+\.\d+\.\d+-[1-9]\d*", entry["tag"]), entry


def test_the_infrastructure_defaults_read_the_stacks_from_the_list():
    defaults = _defaults()
    for key in ("gatus_stack", "healthchecks_stack", "beszel_hub_stack"):
        assert "default_apps" in defaults[key]
    for gone in ("gatus_compose_name", "healthchecks_compose_name", "beszel_hub_compose_name",
                 "gatus_image_tag", "healthchecks_image_tag", "beszel_image_tag",
                 "gatus_image", "healthchecks_image", "beszel_image",
                 "healthchecks_swarm_secrets", "beszel_agent_image_tag", "beszel_agent_image",
                 "beszel_agent_compose_name", "beszel_agent_port", "beszel_agent_swarm_secrets",
                 "beszel_hc_shim_image_tag", "beszel_hc_shim_image", "beszel_hc_shim_compose_name",
                 "beszel_hc_shim_swarm_secrets", "beszel_seed_state_file",
                 "beszel_oidc_display_name"):
        assert gone not in defaults, f"{gone} is back"


def test_the_list_file_has_the_four_fields_and_no_secret():
    template = (INFRA / "templates" / "default-apps.json.j2").read_text()
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.filters["to_nice_json"] = lambda v: json.dumps(v, indent=2)
    env.filters["combine"] = lambda a, b: {**a, **b}
    gv = _group_vars()
    got = json.loads(env.from_string(template).render(
        default_apps=gv["default_apps"], default_apps_repository=gv["default_apps_repository"]))
    assert [set(e) for e in got] == [{"repository", "app", "tag", "stack"}] * 3
    assert {e["repository"] for e in got} == {"https://github.com/catenahq/catena-templates"}
    assert [(e["app"], e["tag"], e["stack"]) for e in got] == [
        (a["app"], a["tag"], a["stack"]) for a in gv["default_apps"]]


def test_the_list_file_is_world_readable():
    write = next(t for t in _tasks("default_apps.yml")
                 if "ansible.builtin.template" in t)["ansible.builtin.template"]
    assert write["dest"] == "{{ default_apps_list_path }}"
    assert write["mode"] == "0644"
    assert _defaults()["default_apps_list_path"] == "/etc/catena/default-apps.json"


# --- the request --------------------------------------------------------------

def test_the_verb_runs_held_with_its_request_on_stdin():
    task = next(t for t in _tasks("default_app.yml") if "ansible.builtin.command" in t)
    assert task["ansible.builtin.command"]["argv"] == [
        "{{ catena_apps_bin }}", "defaults", "-held", "-stdin"]
    assert _defaults()["catena_apps_bin"] == "/usr/local/bin/catena-apps"
    stdin = task["ansible.builtin.command"]["stdin"]
    for key in ("app", "tag", "stack", "env"):
        assert f"'{key}':" in stdin
    assert "preset" not in stdin
    assert task["no_log"] is True, "the env holds secrets"


def test_exits_3_and_4_do_not_fail_the_converge_but_anything_else_does():
    """The command never fails itself (it runs no_log, which would hide the
    reason); the engine's lines print, then a fail task stops the play. Exit 3
    is a tag that cannot be fetched, exit 4 is printed the same way."""
    tasks = _tasks("default_app.yml")
    names = [t["name"] for t in tasks]
    command = next(t for t in tasks if "ansible.builtin.command" in t)
    assert command["failed_when"] is False and command["no_log"] is True
    stop = next(t for t in tasks if "ansible.builtin.fail" in t)
    assert "_default_app.rc | default(1) not in [0, 3, 4]" in stop["when"]
    shown = next(i for i, n in enumerate(names) if n.endswith("what the engine did"))
    assert names.index(command["name"]) < shown < names.index(stop["name"])
    assert "stderr_lines" in stop["ansible.builtin.fail"]["msg"]


def test_nothing_prints_the_request_or_the_registered_result():
    """The registered result carries the request, which holds the mail password
    and the Gatus alert key."""
    for task in _tasks("default_app.yml"):
        for module in ("ansible.builtin.debug", "ansible.builtin.fail"):
            text = json.dumps(task.get(module, {}))
            assert "stdin" not in text and "default_app_env" not in text, task["name"]
            assert "_default_app }}" not in text and "_default_app.cmd" not in text


def test_the_engines_lines_are_printed_unchanged():
    shown = next(t for t in _tasks("default_app.yml") if "ansible.builtin.debug" in t)
    assert shown["ansible.builtin.debug"]["msg"] == (
        "{{ (_default_app.stdout_lines | default([])) + (_default_app.stderr_lines | default([])) }}")


def test_each_app_is_added_with_its_own_entry():
    for name, app in (("gatus.yml", "gatus"), ("healthchecks.yml", "healthchecks"),
                      ("beszel.yml", "beszel")):
        add = next(t for t in _tasks(name)
                   if "default_app.yml" in str(t.get("ansible.builtin.include_tasks")))
        assert add["vars"]["default_app"] == (
            "{{ default_apps | selectattr('app', 'eq', '" + app + "') | first }}")


# --- the env map ------------------------------------------------------------

def test_the_env_map_holds_no_key_the_blueprint_declares():
    for app, env in _env_maps().items():
        assert not set(env) & DECLARED, (app, sorted(set(env) & DECLARED))


@pytest.mark.skipif(not TEMPLATES.is_dir(), reason="the catena-templates checkout is not beside this one")
def test_the_env_map_holds_no_key_the_checked_out_blueprint_declares():
    for app, env in _env_maps().items():
        block = TEMPLATES / app / "x-catena.yml"
        if not block.is_file():
            continue
        declared = set((yaml.safe_load(block.read_text()) or {}).get("env") or {})
        assert not set(env) & declared, (app, sorted(set(env) & declared))


def test_the_env_map_is_the_contracts_core_writes_less_what_the_block_declares():
    """The keys the converge writes are the ones the contract says core owns,
    except any the block declares (its generated and managed values, such as
    SECRET_KEY, HC_PING_KEY and the mail password), which the engine refuses."""
    for app, env in _env_maps().items():
        want = set(CONTRACT[app]["env_core_writes"]) - DECLARED - _block_declared(app)
        assert set(env) == want, app


def test_healthchecks_mail_password_is_not_written_by_the_converge():
    assert "EMAIL_HOST_PASSWORD" not in _env_maps()["healthchecks"]


def test_every_env_value_is_a_string():
    """The request carries strings, as a .env holds them."""
    for app, env in _env_maps().items():
        for key, value in env.items():
            assert isinstance(value, str), (app, key, value)


def test_the_hub_trusts_the_header_and_its_subnet_together():
    env = _env_maps()["beszel"]
    assert {"TRUSTED_AUTH_HEADER", "TRUSTED_PROXY_IPS"} <= set(env)


def test_the_hub_is_told_the_realm_its_sign_in_button_uses():
    env = _env_maps()["beszel"]
    jenv = jinja2.Environment(undefined=jinja2.StrictUndefined)
    assert jenv.from_string(env["HUB_OIDC_ISSUER"]).render(
        keycloak_hostname="auth.acme.test", keycloak_realm="vps"
    ) == "https://auth.acme.test/realms/vps"


def test_healthchecks_names_its_project_after_the_host():
    assert _env_maps()["healthchecks"]["HC_PROJECT_NAME"] == "{{ inventory_hostname }}"


def test_the_secrets_the_contract_names_are_minted_or_the_mail_password():
    minted = (ANSIBLE / "helpers" / "onbox_config.py").read_text()
    for entry in CONTRACT.values():
        for name in entry["secrets"]:
            if name != "smtp_password":
                assert f'"{name}"' in minted, f"{name} is not minted"
    for gone in ("healthchecks_superuser_password", "beszel_universal_token"):
        assert f'"{gone}"' not in minted, f"{gone} is minted again"


def test_gatus_is_given_the_status_address_its_alert_needs():
    env = _env_maps()["gatus"]
    assert set(env) == {"STATUS_HOST"}
    jenv = jinja2.Environment(undefined=jinja2.StrictUndefined)
    jenv.filters["bool"] = lambda v: v is True or str(v).strip().lower() in ("1", "true", "yes", "on")

    def host(deferred: bool) -> str:
        return jenv.from_string(env["STATUS_HOST"]).render(
            catena_public_surface_deferred=deferred,
            infrastructure_gatus_hostname="gatus.acme.test")

    assert host(False) == "gatus.acme.test"
    assert host(True) == "", "no domain yet: a link to `gatus.` would be malformed"



# --- the apps set themselves up ------------------------------------------------

def test_no_task_seeds_deploys_an_agent_or_reads_a_container():
    """The configure hooks of the apps do the setup, so the converge only adds
    each app and writes its values."""
    for name in ("gatus.yml", "healthchecks.yml", "beszel.yml"):
        text = (TASKS / name).read_text()
        for gone in ("docker exec", "docker cp", "docker ps", "swarm_stack.yml",
                     "swarm_secrets", "-seed.py", "hc-shim", "universal_token", "ssh-keygen"):
            assert gone not in text, f"{name} still holds {gone}"
    assert not (TASKS.parent / "templates" / "beszel-agent.compose.yml.j2").exists()
    assert not (TASKS.parent / "templates" / "beszel-hc-shim.compose.yml.j2").exists()
    for script in ("healthchecks-seed.py", "beszel-seed.py", "beszel-hc-shim.py"):
        assert not (ANSIBLE / "scripts" / script).exists(), script


def test_the_running_flag_is_read_from_the_swarm_service():
    tasks = _tasks("default_app.yml")
    ls = next(t for t in tasks if t.get("name", "").endswith("read its swarm service"))
    argv = ls["ansible.builtin.command"]["argv"]
    assert argv[:3] == ["docker", "service", "ls"]
    assert "name={{ default_app.stack }}_app" in argv
    pin = next(t for t in tasks if "_default_app_running" in (t.get("ansible.builtin.set_fact") or {}))
    text = pin["ansible.builtin.set_fact"]["_default_app_running"]
    env = jinja2.Environment()
    env.filters["bool"] = lambda v: v is True or str(v).strip().lower() in ("1", "true", "yes", "on")
    env.filters["regex_escape"] = re.escape
    env.tests["match"] = lambda value, pattern: re.match(pattern, value) is not None
    render = lambda lines: env.from_string(text).render(  # noqa: E731
        _default_apps_available=True, default_app={"stack": "healthchecks"},
        _default_app_service={"stdout_lines": lines}).strip()
    assert render(["healthchecks_app 1/1"]) == "True"
    assert render(["healthchecks_app 0/1"]) == "True"
    assert render(["healthchecks_app 0/0"]) == "False"
    assert render([]) == "False"
    assert render(["healthchecks_appx 1/1"]) == "False"


def test_gatus_comes_after_the_apps_it_reads():
    names = [t["name"] for t in yaml.safe_load((TASKS / "main.yml").read_text())]
    gatus = next(i for i, n in enumerate(names) if n.startswith("Gatus"))
    assert next(i for i, n in enumerate(names) if n.startswith("Healthchecks")) < gatus
    assert next(i for i, n in enumerate(names) if n.startswith("Beszel")) < gatus


# --- the overlays and the config volume ----------------------------------------

def test_the_gate_network_is_a_capability_name():
    gv = _group_vars()
    assert gv["beszel_gate_network"] == "catena-beszelgate"
    for key in ("healthchecks_network", "beszel_network", "beszel_gate_network"):
        assert re.fullmatch(r"catena-[a-z]+", gv[key]), (key, gv[key])


def test_gatus_syncs_into_the_apps_config_volume():
    value = _defaults()["gatus_host_config_dir"]
    assert value == ("{{ storage_mount_point }}/docker/volumes/{{ gatus_stack }}_config/_data")
    gatus = CONTRACT["gatus"]
    folder = jinja2.Environment().from_string(value).render(
        storage_mount_point=STORAGE, gatus_stack=STACKS["gatus"])
    assert folder == f"{STORAGE}/docker/volumes/{gatus['volumes'][gatus['config_volume']]}/_data"
    assert folder == "/srv/catena/docker/volumes/catena-gatus_config/_data"
    env = (INFRA / "templates" / "gatus-sync.env.j2").read_text()
    assert "{{ gatus_host_config_dir }}/50-catena-apps.yaml" in env
    assert "{{ gatus_host_config_dir }}/40-infra.yaml" in env


def test_the_sqlite_list_is_the_contracts():
    expr = _defaults()["infrastructure_sqlite_databases"]
    env = jinja2.nativetypes.NativeEnvironment()
    env.filters["regex_replace"] = lambda value, pattern, repl: re.sub(pattern, repl, value)
    defaults = _defaults()
    got = env.from_string(expr).render(
        **{**defaults, "gatus_stack": STACKS["gatus"],
           "healthchecks_stack": STACKS["healthchecks"], "beszel_hub_stack": STACKS["beszel"]})
    want = [f"{entry['volumes'][db['volume']]}/_data/{db['file']}"
            for entry in (CONTRACT[a] for a in ("gatus", "healthchecks", "beszel"))
            for db in entry["sqlite"]]
    assert got == want


def test_each_loopback_port_and_alias_is_the_contracts():
    defaults = _defaults()
    ports = {
        "gatus": ("gatus_host_localhost_port", "gatus_internal_port", "gatus_network_alias"),
        "healthchecks": ("healthchecks_host_localhost_port", "healthchecks_internal_port",
                         "healthchecks_network_alias"),
        "beszel": ("beszel_hub_host_localhost_port", "beszel_hub_internal_port",
                   "beszel_hub_network_alias"),
    }
    for app, (host_key, internal_key, alias_key) in ports.items():
        [port] = CONTRACT[app]["services"]["app"]["ports"]
        assert defaults[host_key] == port["published"], app
        assert defaults[internal_key] == port["target"], app
        aliases = [a for nets in [CONTRACT[app]["services"]["app"]["networks"]]
                   for names in nets.values() for a in names]
        assert defaults[alias_key] in aliases, app


def test_each_fragment_declares_the_contracts_published_port():
    for name, key, owner in (("gatus.yml", "gatus_host_localhost_port", "gatus"),
                             ("healthchecks.yml", "healthchecks_host_localhost_port", "healthchecks"),
                             ("beszel.yml", "beszel_hub_host_localhost_port", "beszel")):
        text = (TASKS / name).read_text()
        assert f'"port": {key} | int' in text, name
        assert f'"owner": "{owner}"' in text, name


def test_the_overlays_are_the_contracts_networks():
    gv = _group_vars()
    networks = {n for entry in CONTRACT.values()
                for svc in entry["services"].values() for n in (svc.get("networks") or {})}
    for key in ("healthchecks_network", "beszel_network", "beszel_gate_network"):
        assert gv[key] in networks, key
    gate = CONTRACT["beszel"]["services"]["app"]["networks"][gv["beszel_gate_network"]]
    assert gate == [_defaults()["beszel_hub_network_alias"]]


def test_every_infra_spec_stack_is_a_contract_stack():
    text = (INFRA / "templates" / "gatus-infra-spec.json.j2").read_text()
    names = re.findall(r'"stack": "\{\{ (\w+) \}\}"', text)
    assert names, "the infra spec names no stack"
    by_var = {"gatus_stack": "gatus", "healthchecks_stack": "healthchecks",
              "beszel_hub_stack": "beszel"}
    for name in names:
        assert name in by_var, name
        assert STACKS[by_var[name]] in set(STACKS.values())
    assert set(names) == set(by_var)


# --- the address label ----------------------------------------------------------

def test_the_dotenv_filter_reads_every_spelling_the_engine_writes():
    read = _load_filter()
    assert read("SUBDOMAIN=mon\n", "SUBDOMAIN", "x") == "mon"
    assert read("A=1\nSUBDOMAIN='mon'\n", "SUBDOMAIN", "x") == "mon"
    assert read('SUBDOMAIN="mon"\n', "SUBDOMAIN", "x") == "mon"
    assert read("SUBDOMAIN=old\nSUBDOMAIN=new\n", "SUBDOMAIN", "x") == "new"
    assert read("# SUBDOMAIN=no\nOTHER=1\n", "SUBDOMAIN", "x") == "x"
    assert read("SUBDOMAIN=\n", "SUBDOMAIN", "x") == "x"
    assert read("", "SUBDOMAIN", "x") == "x"
    assert read(None, "SUBDOMAIN", "x") == "x"
    assert read("XSUBDOMAIN=no\n", "SUBDOMAIN", "x") == "x"


def test_the_pre_task_sets_the_three_facts_group_vars_reads():
    gv = _group_vars()
    tasks = yaml.safe_load((PLAYBOOKS / "tasks" / "default_app_subdomains.yml").read_text())
    facts = [t["ansible.builtin.set_fact"] for t in tasks if "ansible.builtin.set_fact" in t][-1]
    assert set(facts) == {"default_app_gatus_subdomain", "default_app_healthchecks_subdomain",
                          "default_app_beszel_subdomain"}
    for var in facts:
        assert var in str(gv), f"group_vars no longer reads {var}"
    read = next(t for t in tasks if "ansible.builtin.slurp" in t)
    assert read["ansible.builtin.slurp"]["src"] == (
        "{{ default_apps_compose_dir }}/{{ item.stack }}/.env")
    assert read["failed_when"] is False, "an absent folder is the default"
    assert gv["default_apps_compose_dir"] == "{{ storage_mount_point }}/compose"


def _pre_task(folders: dict[str, str]) -> dict:
    """Run the pre-task's facts: `folders` maps an app to the text of its .env
    (an app absent from it has no folder)."""
    import base64

    tasks = yaml.safe_load((PLAYBOOKS / "tasks" / "default_app_subdomains.yml").read_text())
    task = next(t for t in tasks if "ansible.builtin.set_fact" in t)
    env = jinja2.Environment()
    env.filters["b64decode"] = lambda s: base64.b64decode(s).decode()
    env.filters["zip"] = lambda a, b: zip(a, b)
    env.filters["dotenv_value"] = _load_filter()
    results = []
    for entry in _group_vars()["default_apps"]:
        item = {"item": entry}
        if entry["app"] in folders:
            item["content"] = base64.b64encode(folders[entry["app"]].encode()).decode()
        else:
            item["failed"] = True
        results.append(item)
    texts = env.from_string(task["vars"]["_texts"]).render(
        _default_app_env={"results": results}).strip()
    texts = ast.literal_eval(texts)
    labels = task["ansible.builtin.set_fact"]["_default_app_labels"]
    return {name: env.from_string(value).render(_texts=texts).strip()
            for name, value in labels.items()}


def _label_check(labels: dict) -> list[str]:
    """The failure messages of the pre-task's assert, one per refused label."""
    tasks = yaml.safe_load((PLAYBOOKS / "tasks" / "default_app_subdomains.yml").read_text())
    check = next(t for t in tasks if "ansible.builtin.assert" in t)
    env = jinja2.Environment()
    env.tests["match"] = lambda value, pattern: re.match(pattern, value) is not None
    refused = []
    for key, value in labels.items():
        item = {"key": key, "value": value}
        holds = env.compile_expression(check["ansible.builtin.assert"]["that"])(item=item)
        if not holds:
            refused.append(env.from_string(check["ansible.builtin.assert"]["fail_msg"]).render(item=item))
    return refused


def test_the_label_is_the_folders_subdomain_or_the_apps_own_name():
    labels = _pre_task({"gatus": "A=1\nSUBDOMAIN=status\n", "beszel": "A=1\n"})
    assert labels == {"gatus": "status", "healthchecks": "healthchecks", "beszel": "beszel"}
    assert _label_check(labels) == []


def test_no_folder_leaves_each_app_its_own_name():
    labels = _pre_task({})
    assert labels == {"gatus": "gatus", "healthchecks": "healthchecks", "beszel": "beszel"}
    assert _label_check(labels) == []


def test_a_label_that_is_one_dns_label_is_accepted():
    for value in ("a", "hc", "my-status", "x9", "a" * 63):
        assert _label_check({"gatus": value}) == [], value


def test_a_label_that_is_not_one_dns_label_stops_the_converge():
    """A dot, a quote, a backtick, a slash or an uppercase letter would reach the
    gate's route, the Keycloak redirect URI, the Gatus probes and the allowed
    hosts."""
    for value in ("hc.old", "a'b", 'a"b', "a`b", "a/b", "a b", "-a", "a-", "Hc",
                  "a" * 64, "a;b", "$(x)", "a{b}"):
        [message] = _label_check({"healthchecks": _pre_task({"healthchecks": f"SUBDOMAIN={value}\n"})["healthchecks"]}) or [""]
        assert "healthchecks app" in message and "one DNS label" in message, value


def test_the_env_files_are_never_printed():
    tasks = yaml.safe_load((PLAYBOOKS / "tasks" / "default_app_subdomains.yml").read_text())
    for task in tasks:
        if "ansible.builtin.slurp" in task or "_default_app_labels" in (
                task.get("ansible.builtin.set_fact") or {}):
            assert task["no_log"] is True, task["name"]


def test_no_saved_setting_is_read_from_the_store():
    text = (PLAYBOOKS / "tasks" / "default_app_subdomains.yml").read_text()
    assert "config.json" not in text and "_SUBDOMAIN" not in text


def test_every_converge_path_reads_the_label_before_any_role():
    for name in ("converge.yml", "reconcile.yml", "validate.yml"):
        plays = yaml.safe_load((PLAYBOOKS / name).read_text())
        found = [p for p in plays if any(
            str((t.get("ansible.builtin.include_tasks") or {}).get("file")
                if isinstance(t.get("ansible.builtin.include_tasks"), dict)
                else t.get("ansible.builtin.include_tasks")).endswith(
                    "default_app_subdomains.yml")
            for t in p.get("pre_tasks", []))]
        assert found, f"{name} does not read the address label of the default apps"
        for play in found:
            task = next(t for t in play["pre_tasks"] if "default_app_subdomains" in str(t))
            assert "always" in task["tags"], f"{name} skips it under --tags"


def test_the_subdomains_are_no_longer_store_keys():
    knobs = (ANSIBLE / "helpers" / "knobs.yml").read_text()
    for gone in ("GATUS_SUBDOMAIN", "HEALTHCHECKS_SUBDOMAIN", "BESZEL_SUBDOMAIN"):
        assert gone not in knobs, gone
