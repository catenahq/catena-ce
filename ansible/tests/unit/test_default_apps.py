"""Gatus, Healthchecks and Beszel's hub are catalog apps that
reconcile/roles/infrastructure adds with `catena-apps defaults`.

The properties held here:

- each app's stack keeps the name its swarm stack had, so volumes, the SQLite
  copy list and the update engine's skip set are unchanged;
- `/etc/catena/default-apps.json` lists the apps, with no secret;
- the env map the converge writes holds no key the app's own x-catena block
  declares (the engine refuses one);
- the seeds and the agent that need an app run only while the app runs;
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
import pytest
import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
REPO = ANSIBLE.parent
INFRA = ANSIBLE / "reconcile" / "roles" / "infrastructure"
TASKS = INFRA / "tasks"
PLAYBOOKS = ANSIBLE / "playbooks"
GROUP_VARS = PLAYBOOKS / "group_vars" / "all" / "main.yml"
TEMPLATES = REPO.parent / "catena-templates" / "blueprints"

# The stack each app had as a swarm stack.
STACKS = {"gatus": "gatus", "healthchecks": "healthchecks", "beszel": "beszel-hub"}

# What the apps' x-catena blocks declare (settings, generated secrets and
# managed values); the engine refuses a request that writes one of these in env.
DECLARED = {"SUBDOMAIN", "HC_URL", "HUB_URL", "UI_TITLE", "SECRET_KEY"}


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


def _load_filter():
    spec = importlib.util.spec_from_file_location(
        "dotenv_value", PLAYBOOKS / "filter_plugins" / "dotenv_value.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.dotenv_value


# --- the list ---------------------------------------------------------------

def test_every_entry_keeps_the_name_its_stack_had():
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
                 "healthchecks_swarm_secrets"):
        assert gone not in defaults, f"{gone} is back"
    for kept in ("beszel_agent_image_tag", "beszel_agent_compose_name",
                 "beszel_hc_shim_image_tag", "beszel_hc_shim_compose_name"):
        assert kept in defaults, f"{kept} is gone"


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


def test_an_unfetchable_tag_does_not_fail_the_converge_but_anything_else_does():
    """The command never fails itself (it runs no_log, which would hide the
    reason); the engine's lines print, then a fail task stops the play."""
    tasks = _tasks("default_app.yml")
    names = [t["name"] for t in tasks]
    command = next(t for t in tasks if "ansible.builtin.command" in t)
    assert command["failed_when"] is False and command["no_log"] is True
    stop = next(t for t in tasks if "ansible.builtin.fail" in t)
    assert "_default_app.rc | default(1) not in [0, 3]" in stop["when"]
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


def test_the_hubs_and_healthchecks_keys_the_blocks_generate_are_not_written():
    """The block generates SECRET_KEY, so the converge never writes it."""
    assert "SECRET_KEY" not in _env_maps()["healthchecks"]


def test_every_env_value_is_a_string():
    """The request carries strings, as a .env holds them."""
    for app, env in _env_maps().items():
        for key, value in env.items():
            assert isinstance(value, str), (app, key, value)


def test_the_hub_trusts_the_header_and_its_subnet_together():
    env = _env_maps()["beszel"]
    assert {"TRUSTED_AUTH_HEADER", "TRUSTED_PROXY_IPS"} <= set(env)


def test_gatus_is_given_the_ping_key_and_status_address_its_alert_needs():
    env = _env_maps()["gatus"]
    assert set(env) == {"HC_PING_KEY", "STATUS_HOST"}
    jenv = jinja2.Environment(undefined=jinja2.StrictUndefined)
    jenv.filters["bool"] = lambda v: v is True or str(v).strip().lower() in ("1", "true", "yes", "on")

    def host(deferred: bool) -> str:
        return jenv.from_string(env["STATUS_HOST"]).render(
            catena_public_surface_deferred=deferred,
            infrastructure_gatus_hostname="gatus.acme.test")

    assert host(False) == "gatus.acme.test"
    assert host(True) == "", "no domain yet: a link to `gatus.` would be malformed"



# --- seeds wait for the app ---------------------------------------------------

def _after(name: str, marker: str) -> list[dict]:
    tasks = _tasks(name)
    first = next(i for i, t in enumerate(tasks) if t.get("name") == marker)
    return tasks[first + 1:]


def _guarded(task: dict, flag: str) -> bool:
    cond = task.get("when")
    conds = cond if isinstance(cond, list) else [cond]
    return flag in [str(c).strip() for c in conds]


def test_the_healthchecks_seed_and_its_wait_run_only_while_it_runs():
    flag = "_healthchecks_running | bool"
    tasks = _after("healthchecks.yml", "Healthchecks: not running -- nothing is seeded")
    for task in tasks:
        text = json.dumps(task)
        if any(word in text for word in ("docker ps", "docker exec", "docker service", "fail")):
            assert _guarded(task, flag), task["name"]
    named = {t["name"]: t for t in tasks}
    assert _guarded(named["Healthchecks: run the Django seed script"], flag)
    assert _guarded(named["Healthchecks: find running container"], flag)


def test_the_hub_key_the_seed_and_the_agent_run_only_while_the_hub_runs():
    flag = "_beszel_hub_running | bool"
    named = {t["name"]: t for t in _tasks("beszel.yml")}
    for name in ("Beszel: wait for hub container",
                 "Beszel: derive hub public key for the agent",
                 "Beszel: pin derived hub public key as a fact",
                 "Beszel: seed the universal token + OIDC login (superuser-authenticated)",
                 "Beszel: deploy agent as a swarm stack",
                 "Beszel: seed the default alert rules once the agent has registered"):
        assert _guarded(named[name], flag), name


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
    env = (INFRA / "templates" / "gatus-sync.env.j2").read_text()
    assert "{{ gatus_host_config_dir }}/50-catena-apps.yaml" in env
    assert "{{ gatus_host_config_dir }}/40-infra.yaml" in env


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
    assert set(facts) == {"cfg_gatus_subdomain", "cfg_healthchecks_subdomain",
                          "cfg_beszel_subdomain"}
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
