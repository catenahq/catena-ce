"""An application's own sign-in button turns away an account outside the tiers
its flow admits, and the flows are never left half built.

keycloak_signin_flows (playbooks/filter_plugins) builds the flows
realm-vps.yaml.j2 declares: catena-signin-admin and catena-signin-staff, which
dashboard-sync binds an application's own sign-in entry to by tier, and
catena-browser, the realm's own. Each tier flow runs every step of the realm's
built-in browser flow in a required sub-flow, then a conditional sub-flow whose
last step denies an account holding none of the tier roles the flow admits. The
login form is one screen, username and password, with the emailed code as its
alternative for a client (not in the administrators' flow), then the
conditional second factor.

Every top-level flow has the alias <name>-v<8 hex>, which its sub-flows and
configs embed. keycloak-config-cli creates a flow under an alias it does not
hold as a new flow and binds the realm's browser flow after every flow exists,
so an import that stops half way leaves the realm on the previous complete
version. After an import the converge records a digest of each flow as Keycloak
holds it; a flow that reads differently, or is gone, makes the next converge
build every flow under the next generation. The versions no entry names are
deleted last.

keycloak-config-cli rebuilds a flow whose executions differ from the file once
both are sorted by priority, and Keycloak gives a sub-flow it creates the next
priority after the last execution: the file lists every execution in priority
order, so what Keycloak builds compares equal to it and later converges leave
the flow alone.

realm_bootstrap.yml reads the two flow ids after the import and fails without
them; dashboard-sync's env file carries them.

Run: uv run pytest tests/unit/test_realm_signin_flows.py
"""
from __future__ import annotations

import json
import re
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template

from test_realm_bootstrap_settles import BOOTSTRAP, ROLE, _render
import keycloak_signin_flows as F

ANSIBLE = Path(__file__).resolve().parents[2]
ENV_TEMPLATE = (ANSIBLE / "reconcile" / "roles" / "infrastructure" / "templates"
                / "dashboard-sync.env.j2")
EXECUTIONS = ROLE / "tasks" / "_signin_flow_executions.yml"

# The refusal step: the id of the extension's authenticator
# (reconcile/roles/keycloak/provider).
REFUSAL = "catena-tier-refusal"

ADMITTED = {
    "catena-signin-admin": {"catena-tier-admin"},
    "catena-signin-staff": {"catena-tier-admin", "catena-tier-staff"},
}

# The flow for applications open to every account, which the realm signs in
# through, and the step that mails a client a code.
BROWSER = "catena-browser"
POST_BROKER = "catena-post-broker-login"
EMAIL_CODE = "catena-client-email-code"
PASSWORD_FORM = "auth-username-password-form"
VERSIONED = re.compile(r"^(catena-[a-z-]+)-v[0-9a-f]{8}$")

# The realm's built-in browser flow on a Keycloak 26.6 host, step by step:
# (sub-flow, authenticator or nested sub-flow, requirement).
SIGN_IN_STEPS = [
    ("sign-in", "auth-cookie", "ALTERNATIVE"),
    ("sign-in", "auth-spnego", "DISABLED"),
    ("sign-in", "identity-provider-redirector", "ALTERNATIVE"),
    ("sign-in", "@organization", "ALTERNATIVE"),
    ("sign-in", "@forms", "ALTERNATIVE"),
]
COMMON_STEPS = [
    ("organization", "@conditional organization", "CONDITIONAL"),
    ("conditional organization", "conditional-user-configured", "REQUIRED"),
    ("conditional organization", "organization", "ALTERNATIVE"),
    ("forms", "@conditional 2FA", "CONDITIONAL"),
    ("conditional 2FA", "conditional-user-configured", "REQUIRED"),
    ("conditional 2FA", "conditional-credential", "REQUIRED"),
    ("conditional 2FA", "auth-otp-form", "ALTERNATIVE"),
    ("conditional 2FA", "webauthn-authenticator", "DISABLED"),
    ("conditional 2FA", "auth-recovery-authn-code-form", "DISABLED"),
]
FORM_STEPS = COMMON_STEPS + [
    ("forms", "@credential", "REQUIRED"),
    ("credential", PASSWORD_FORM, "ALTERNATIVE"),
    ("credential", EMAIL_CODE, "ALTERNATIVE"),
]
# The administrators' flow asks the combined form for the password alone.
ADMIN_FORM_STEPS = COMMON_STEPS + [("forms", PASSWORD_FORM, "REQUIRED")]
FORM_SUBFLOWS = ("organization", "conditional organization", "forms", "credential",
                 "conditional 2FA")


def _realm() -> dict:
    return yaml.safe_load(_render(realm_seed=False))


def _flows() -> dict[str, dict]:
    return {f["alias"]: f for f in _realm()["authenticationFlows"]}


def _aliases() -> dict[str, str]:
    """The versioned alias of each top-level flow, by its name without version."""
    return {VERSIONED.match(f["alias"]).group(1): f["alias"]
            for f in _realm()["authenticationFlows"] if f["topLevel"]}


def _step(flow: str, execution: dict) -> str:
    sub = execution.get("flowAlias")
    return "@" + sub[len(flow) + 1:] if sub else execution["authenticator"]


def test_the_tier_flows_and_the_realm_browser_flow_are_top_level():
    flows = _flows()
    for name in [*ADMITTED, BROWSER]:
        flow = flows[_aliases()[name]]
        assert flow["topLevel"] is True
        assert flow["builtIn"] is False
        assert flow["providerId"] == "basic-flow"
    assert all(not f["builtIn"] for f in flows.values())


def test_the_realm_signs_in_through_the_versioned_catena_browser():
    assert _realm()["browserFlow"] == _aliases()[BROWSER]
    assert _realm()["browserFlow"] == F.keycloak_signin_flows(0)["aliases"]["browser"]


def _collect(alias: str, top: dict, subflows) -> list:
    """The steps of a flow: its top-level entry (a sub-flow of the tier flows,
    the flow itself for catena-browser), then its login-form sub-flows."""
    flows = _flows()
    got = [(sub, _step(alias, e), e["requirement"])
           for sub, flow in top.items() for e in flow["authenticationExecutions"]]
    for sub in subflows:
        for e in flows[f"{alias} {sub}"]["authenticationExecutions"]:
            got.append((sub, _step(alias, e), e["requirement"]))
    return got


def test_each_copy_keeps_every_step_of_the_browser_flow():
    flows, aliases = _flows(), _aliases()
    for name in ADMITTED:
        alias = aliases[name]
        admin = name == "catena-signin-admin"
        want = ADMIN_FORM_STEPS if admin else FORM_STEPS
        subflows = [s for s in FORM_SUBFLOWS if not (admin and s == "credential")]
        got = _collect(alias, {"sign-in": flows[f"{alias} sign-in"]}, subflows)
        assert sorted(got) == sorted(SIGN_IN_STEPS + want), name
        assert f"{alias} credential" in flows or admin


def test_the_realm_browser_flow_lists_the_same_steps_without_a_tier_check():
    flows, alias = _flows(), _aliases()[BROWSER]
    got = _collect(alias, {"sign-in": flows[alias]}, FORM_SUBFLOWS)
    assert sorted(got) == sorted(SIGN_IN_STEPS + FORM_STEPS)
    assert f"{alias} tier check" not in flows


def test_no_flow_asks_for_the_username_alone():
    for flow in _flows().values():
        for e in flow["authenticationExecutions"]:
            assert e.get("authenticator") != "auth-username-form", flow["alias"]


def test_the_emailed_code_is_an_alternative_to_the_password_never_to_the_second_factor():
    flows, aliases = _flows(), _aliases()
    for name in [BROWSER, "catena-signin-staff"]:
        alias = aliases[name]
        forms = flows[f"{alias} forms"]["authenticationExecutions"]
        assert [(e.get("authenticator") or e["flowAlias"], e["requirement"], e["priority"])
                for e in forms] == [(f"{alias} credential", "REQUIRED", 10),
                                    (f"{alias} conditional 2FA", "CONDITIONAL", 20)]
        credential = flows[f"{alias} credential"]["authenticationExecutions"]
        assert [(e["authenticator"], e["requirement"], e["priority"]) for e in credential] == [
            (PASSWORD_FORM, "ALTERNATIVE", 10), (EMAIL_CODE, "ALTERNATIVE", 20)]
        second = {e["authenticator"] for e in flows[f"{alias} conditional 2FA"]["authenticationExecutions"]}
        assert EMAIL_CODE not in second


def test_the_administrators_flow_asks_for_the_password_and_never_the_code():
    flows, alias = _flows(), _aliases()["catena-signin-admin"]
    forms = flows[f"{alias} forms"]["authenticationExecutions"]
    assert [(e.get("authenticator") or e["flowAlias"], e["requirement"]) for e in forms] == [
        (PASSWORD_FORM, "REQUIRED"), (f"{alias} conditional 2FA", "CONDITIONAL")]
    assert f"{alias} credential" not in flows


def test_the_emailed_code_is_offered_in_the_staff_and_realm_flows_only():
    flows, aliases = _flows(), _aliases()
    where = {name for name, flow in flows.items()
             for e in flow["authenticationExecutions"] if e.get("authenticator") == EMAIL_CODE}
    assert where == {f"{aliases[a]} credential" for a in (BROWSER, "catena-signin-staff")}


def test_the_second_factor_condition_matches_the_realm_flow():
    realm, aliases = _realm(), _aliases()
    configs = {c["alias"]: c["config"] for c in realm["authenticatorConfig"]}
    for name in [*ADMITTED, BROWSER]:
        [cond] = [e for e in _flows()[f"{aliases[name]} conditional 2FA"]["authenticationExecutions"]
                  if e["authenticator"] == "conditional-credential"]
        assert configs[cond["authenticatorConfig"]] == {"credentials": "webauthn-passwordless"}


def test_the_tier_flows_run_the_sign_in_steps_first_and_the_deny_step_last():
    flows, aliases = _flows(), _aliases()
    for name in ADMITTED:
        alias = aliases[name]
        top = flows[alias]["authenticationExecutions"]
        assert [(e["flowAlias"], e["requirement"]) for e in top] == [
            (f"{alias} sign-in", "REQUIRED"), (f"{alias} tier check", "CONDITIONAL")]
        steps = flows[f"{alias} tier check"]["authenticationExecutions"]
        assert steps[-1]["authenticator"] == REFUSAL
        assert all(s["requirement"] == "REQUIRED" for s in steps)


def test_the_deny_step_runs_only_for_an_account_without_every_admitted_role():
    realm, aliases = _realm(), _aliases()
    configs = {c["alias"]: c["config"] for c in realm["authenticatorConfig"]}
    for name, admitted in ADMITTED.items():
        conditions = _flows()[f"{aliases[name]} tier check"]["authenticationExecutions"][:-1]
        assert all(c["authenticator"] == "conditional-user-role" for c in conditions)
        roles = {configs[c["authenticatorConfig"]]["condUserRole"] for c in conditions}
        assert roles == admitted, name
        assert all(configs[c["authenticatorConfig"]]["negate"] == "true" for c in conditions)


def test_executions_are_listed_in_priority_order():
    for alias, flow in _flows().items():
        prios = [e["priority"] for e in flow["authenticationExecutions"]]
        assert prios == sorted(prios) and len(set(prios)) == len(prios), alias


def test_every_config_alias_is_unique_and_used_once():
    realm = _realm()
    declared = [c["alias"] for c in realm["authenticatorConfig"]]
    used = [e["authenticatorConfig"] for f in realm["authenticationFlows"]
            for e in f["authenticationExecutions"] if e.get("authenticatorConfig")]
    assert len(declared) == len(set(declared))
    assert sorted(used) == sorted(declared)


def test_every_sub_flow_is_declared_once():
    realm = _realm()
    aliases = [f["alias"] for f in realm["authenticationFlows"]]
    assert len(aliases) == len(set(aliases))
    top = {f["alias"] for f in realm["authenticationFlows"] if f["topLevel"]}
    assert top == set(_aliases().values())
    assert set(_aliases()) == {*ADMITTED, BROWSER, POST_BROKER}
    referenced = [e["flowAlias"] for f in realm["authenticationFlows"]
                  for e in f["authenticationExecutions"] if e.get("authenticatorFlow")]
    assert sorted(referenced) == sorted(a for a in aliases if a not in top)


# --- versions ------------------------------------------------------------------

def test_every_top_level_alias_is_versioned_and_sub_flows_and_configs_derive_from_it():
    realm = _realm()
    tops = set(_aliases().values())
    for flow in realm["authenticationFlows"]:
        if flow["topLevel"]:
            assert VERSIONED.match(flow["alias"]), flow["alias"]
        else:
            assert any(flow["alias"].startswith(top + " ") for top in tops), flow["alias"]
    for config in realm["authenticatorConfig"]:
        assert any(config["alias"].startswith(top + "-") for top in tops), config["alias"]


def test_a_version_is_stable_for_the_same_input():
    assert F.keycloak_signin_flows(0) == F.keycloak_signin_flows(0)
    assert F.keycloak_signin_flows(3)["aliases"] == F.keycloak_signin_flows(3)["aliases"]


def test_a_version_moves_only_for_the_flow_whose_steps_change(monkeypatch):
    before = F.keycloak_signin_flows(0)["aliases"]
    monkeypatch.setitem(F.TIERS, "staff", ["catena-tier-admin", "catena-tier-staff", "catena-tier-extra"])
    after = F.keycloak_signin_flows(0)["aliases"]
    assert after["browser"] == before["browser"]
    assert after["admin"] == before["admin"]
    assert after["staff"] != before["staff"]
    assert after["post_broker_login"] != before["post_broker_login"]


def test_every_version_moves_with_the_generation():
    first, second = F.keycloak_signin_flows(0)["aliases"], F.keycloak_signin_flows(1)["aliases"]
    assert all(first[key] != second[key] for key in first)


def test_the_flows_are_alias_free_in_what_is_versioned():
    """The same tree under another alias gives the same version."""
    flows, configs = F._browser("x")
    other, other_configs = F._browser("y")
    assert json.dumps(flows).replace('"x', '"y') == json.dumps(other)
    assert F._version(F._browser, 0) == F._version(F._browser, 0)


# --- the record -----------------------------------------------------------------

EXECUTIONS_LISTING = [
    {"id": "e1", "level": 0, "index": 0, "priority": 10, "requirement": "ALTERNATIVE",
     "providerId": "auth-cookie", "displayName": "Cookie", "requirementChoices": ["A"]},
    {"id": "e2", "level": 0, "index": 1, "priority": 20, "requirement": "REQUIRED",
     "authenticationFlow": True, "displayName": "forms", "alias": "forms"},
]


FLOW_RECORD = {"id": "f1", "alias": "a", "description": "d", "providerId": "basic-flow",
               "topLevel": True, "builtIn": False}
READ = {"flow": FLOW_RECORD, "executions": EXECUTIONS_LISTING}


def test_the_digest_follows_the_steps_and_ignores_ids():
    base = F.keycloak_flow_digest(READ)
    renamed = [{**e, "id": e["id"] + "x", "requirementChoices": []} for e in EXECUTIONS_LISTING]
    assert F.keycloak_flow_digest({"flow": {**FLOW_RECORD, "id": "other"}, "executions": renamed}) == base
    edited = [{**EXECUTIONS_LISTING[0], "requirement": "DISABLED"}, EXECUTIONS_LISTING[1]]
    assert F.keycloak_flow_digest({**READ, "executions": edited}) != base


def test_a_description_edit_changes_the_digest():
    base = F.keycloak_flow_digest(READ)
    sub = [EXECUTIONS_LISTING[0], {**EXECUTIONS_LISTING[1], "description": "edited"}]
    assert F.keycloak_flow_digest({**READ, "executions": sub}) != base
    assert F.keycloak_flow_digest({"flow": {**FLOW_RECORD, "description": "edited"},
                                   "executions": EXECUTIONS_LISTING}) != base


def test_the_digests_are_read_from_the_printed_lines():
    text = f"a\t{json.dumps(READ)}\nb\tnull\n"
    assert F.keycloak_flow_digests(text) == {"a": F.keycloak_flow_digest(READ), "b": None}


def test_the_generation_moves_on_a_changed_or_missing_flow_only():
    digest = F.keycloak_flow_digest(READ)
    record = {"generation": 4, "flows": {"a": digest, "b": digest}}
    assert F.keycloak_flow_generation({}, {}) == 0
    assert F.keycloak_flow_generation(record, {"a": digest, "b": digest}) == 4
    assert F.keycloak_flow_generation(record, {"a": digest, "b": "other"}) == 5
    assert F.keycloak_flow_generation(record, {"a": digest, "b": None}) == 5
    assert F.keycloak_flow_generation(record, {"a": digest}) == 5


def test_the_record_holds_the_generation_and_every_digest():
    assert F.keycloak_flow_record(2, {"b": "2", "a": "1"}) == {"generation": 2, "flows": {"a": "1", "b": "2"}}


def test_only_unreferenced_old_versions_of_catena_flows_are_stale():
    build = F.keycloak_signin_flows(1)
    current = list(build["aliases"].values())
    old = "catena-browser-v00000000"
    listing = {
        "flows": [{"id": f"id-{a}", "alias": a, "topLevel": True} for a in current] + [
            {"id": "old1", "alias": old, "topLevel": True},
            {"id": "old2", "alias": "catena-signin-staff", "topLevel": True},
            {"id": "old3", "alias": "catena-signin-admin-v11111111", "topLevel": True},
            {"id": "old4", "alias": "catena-post-broker-login-v22222222", "topLevel": True},
            {"id": "old5", "alias": "catena-browser-v00000000 forms", "topLevel": False},
            {"id": "own", "alias": "our-own-flow", "topLevel": True},
            {"id": "browser", "alias": "browser", "topLevel": True}],
        "bound": ["old2", "catena-post-broker-login-v22222222"],
    }
    assert F.keycloak_flow_stale(listing, build) == [
        {"id": "old1", "alias": old}, {"id": "old3", "alias": "catena-signin-admin-v11111111"}]


# --- the converge ---------------------------------------------------------------

def _tasks() -> list[dict]:
    return [t for t in yaml.safe_load(BOOTSTRAP.read_text()) if isinstance(t, dict)]


def _index(fragment: str) -> int:
    names = [str(t.get("name", "")) for t in _tasks()]
    return next(i for i, n in enumerate(names) if fragment in n)


def _task(fragment: str) -> dict:
    return _tasks()[_index(fragment)]


def test_the_drift_is_read_and_the_flows_built_before_either_render():
    read, built = _index("read the flows the record names"), _index("build the sign-in flows")
    generation = _index("settle the generation")
    seed, persistent = _index("seed the admin password"),_index("render realm config")
    assert _index("read the record of the sign-in flows") < read < generation < built < seed < persistent
    assert _task("build the sign-in flows")["ansible.builtin.set_fact"]["keycloak_signin"] == (
        "{{ keycloak_flow_generation | keycloak_signin_flows }}")


def test_the_record_follows_the_import_and_the_cleanup_comes_last():
    names = [str(t.get("name", "")) for t in _tasks()]
    order = [names.index(f"Keycloak realm: {n}") for n in (
        "import the realm config", "the realm names the current post-broker-login flow",
        "identity providers run the current post-broker-login flow",
        "every identity provider runs the tier check after a brokered sign-in", "read the sign-in flow ids",
        "both sign-in flows exist", "read the flows the import built", "record the sign-in flows",
        "read the sign-in flows and what names them", "delete the sign-in flows nothing names")]
    assert order == sorted(order)
    assert names.index("Keycloak realm: delete the sign-in flows nothing names") < len(names) - 1
    assert not any("record the sign-in flows" in n for n in names[:order[0]])


def test_the_converge_reads_both_versioned_flow_ids_and_fails_without_them():
    tasks = _tasks()
    read = _index("read the sign-in flow ids")
    keep, check = _index("keep the sign-in flow ids"), _index("both sign-in flows exist")
    argv = tasks[read]["ansible.builtin.command"]["argv"]
    assert argv[argv.index("get") + 1] == "authentication/flows"
    kept = tasks[keep]["ansible.builtin.set_fact"]["keycloak_signin_flows"]
    assert set(kept) == {"admin", "staff"}
    for tier in kept:
        assert f"keycloak_signin.aliases.{tier}" in kept[tier]
    assert tasks[check]["ansible.builtin.assert"]["that"] == [
        "keycloak_signin_flows.admin | length > 0",
        "keycloak_signin_flows.staff | length > 0"]


def test_dashboard_sync_gets_the_flow_ids():
    lines = [ln for ln in ENV_TEMPLATE.read_text().splitlines()
             if ln.startswith("KEYCLOAK_SIGNIN_FLOWS=")]
    assert lines == ["KEYCLOAK_SIGNIN_FLOWS='{{ keycloak_signin_flows | default({}) | to_json }}'"]


def test_the_attribute_name_is_the_one_the_listener_reads():
    source = (ROLE / "provider" / "src" / "main" / "java" / "run" / "catena" / "keycloak"
              / "CatenaTierBindingListener.java").read_text()
    name = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())["keycloak_realm_post_broker_attribute"]
    assert f'FLOW_ATTRIBUTE = "{name}"' in source


# --- the converge against a stand-in kcadm -------------------------------------

CT = "keycloak_server.1.abc"

FAKE_DOCKER = textwrap.dedent('''\
    #!/usr/bin/env python3
    import json, os, sys
    path = os.environ["FAKE_KC_STATE"]
    state = json.load(open(path))
    args = sys.argv[1:]
    assert args[:3] == ["exec", "keycloak_server.1.abc", "/opt/keycloak/bin/kcadm.sh"], args
    kc = args[3:]
    state["calls"].append(kc)
    json.dump(state, open(path, "w"))
    if kc[0] == "get" and kc[1].startswith("authentication/flows/") and kc[1].endswith("/executions"):
        alias = kc[1].split("/")[2]
        if alias in state["executions"]:
            print(json.dumps(state["executions"][alias], indent=2))
            sys.exit(0)
        sys.exit(1)
    if kc[0] == "get" and kc[1].startswith("authentication/flows/"):
        record = state["records"].get(kc[1].split("/")[2])
        if record is None:
            sys.exit(1)
        print(json.dumps(record, indent=2))
        sys.exit(0)
    if kc[:2] == ["get", "authentication/flows"]:
        print(json.dumps(state["flows"]))
    elif kc[0] == "get" and kc[1].startswith("realms/"):
        print(json.dumps(state["realm"]))
    elif kc[:2] == ["get", "clients"]:
        print(json.dumps(state["clients"]))
    elif kc[:2] == ["get", "identity-provider/instances"] and "--format" in kc:
        for p in state["providers"]:
            print(p["alias"] + "," + p.get("postBrokerLoginFlowAlias", ""))
    elif kc[:2] == ["get", "identity-provider/instances"]:
        print(json.dumps(state["providers"]))
    elif kc[0] == "delete":
        if kc[1].split("/")[-1] in state["refuse"]:
            print("flow in use", file=sys.stderr)
            sys.exit(1)
    elif kc[0] != "update":
        sys.exit("unexpected " + repr(kc))
''')


def _run(tmp_path: Path, task: dict, state_in: dict, variables: dict):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    docker = bindir / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"calls": [], "executions": {}, "flows": [], "records": {}, "realm": {},
                                 "clients": [], "providers": [], "refuse": [], **state_in}))
    script = Templar(loader=DataLoader(), variables={"keycloak_realm": "vps", **variables}).template(
        trust_as_template(task["ansible.builtin.shell"]))
    proc = subprocess.run(["bash", "-c", script], text=True, capture_output=True,
                          env={"PATH": f"{bindir}:/usr/bin:/bin", "FAKE_KC_STATE": str(state)})
    return proc, json.loads(state.read_text())


def _read_executions(tmp_path: Path, aliases: list[str], executions: dict, description: str = "d"):
    """Reads the flows of `aliases` that `executions` holds, each with a record carrying `description`."""
    task = yaml.safe_load(EXECUTIONS.read_text())[0]
    held = [a for a in aliases if a in executions]
    state = {"executions": executions,
             "flows": [{"id": f"id-{a}", "alias": a} for a in held],
             "records": {f"id-{a}": {**FLOW_RECORD, "id": f"id-{a}", "alias": a, "description": description}
                         for a in held}}
    return _run(tmp_path, task, state, {"kc_flow_container": CT, "kc_flow_aliases": aliases})


def _digest_of(alias: str, executions=EXECUTIONS_LISTING, description: str = "d") -> str:
    return F.keycloak_flow_digest({"flow": {**FLOW_RECORD, "alias": alias, "description": description},
                                   "executions": executions})


def test_a_flow_the_realm_holds_is_read_as_a_digest_and_one_it_lacks_as_none(tmp_path):
    proc, state = _read_executions(tmp_path, ["catena-browser-v1", "gone-v2"],
                                   {"catena-browser-v1": EXECUTIONS_LISTING})
    assert proc.returncode == 0, proc.stderr
    assert F.keycloak_flow_digests(proc.stdout) == {
        "catena-browser-v1": _digest_of("catena-browser-v1"), "gone-v2": None}
    assert ["get", "authentication/flows/catena-browser-v1/executions"] in [c[:2] for c in state["calls"]]
    assert ["get", "authentication/flows/id-catena-browser-v1"] in [c[:2] for c in state["calls"]]


def test_a_hand_edit_read_before_the_render_bumps_the_generation(tmp_path):
    record = F.keycloak_flow_record(2, {"catena-browser-v1": _digest_of("catena-browser-v1")})
    edited = [{**EXECUTIONS_LISTING[0], "requirement": "DISABLED"}, EXECUTIONS_LISTING[1]]
    cases = ((EXECUTIONS_LISTING, "d", 2), (edited, "d", 3), (None, "d", 3),
             (EXECUTIONS_LISTING, "edited description", 3))
    for held, description, want in cases:
        proc, _ = _read_executions(tmp_path, list(record["flows"]),
                                   {} if held is None else {"catena-browser-v1": held}, description)
        assert F.keycloak_flow_generation(record, F.keycloak_flow_digests(proc.stdout)) == want


def test_the_record_is_written_from_what_the_import_built(tmp_path):
    build = F.keycloak_signin_flows(2)
    aliases = list(build["aliases"].values())
    proc, _ = _read_executions(tmp_path, aliases, {a: EXECUTIONS_LISTING for a in aliases})
    digests = F.keycloak_flow_digests(proc.stdout)
    assert set(digests) == set(aliases) and None not in digests.values()
    record = F.keycloak_flow_record(2, digests)
    assert F.keycloak_flow_generation(record, digests) == 2


def test_providers_on_an_older_version_move_to_the_current_one(tmp_path):
    current = F.keycloak_signin_flows(1)["aliases"]["post_broker_login"]
    providers = [{"alias": "bare", "postBrokerLoginFlowAlias": POST_BROKER},
                 {"alias": "old", "postBrokerLoginFlowAlias": "catena-post-broker-login-v00000000"},
                 {"alias": "none"},
                 {"alias": "now", "postBrokerLoginFlowAlias": current},
                 {"alias": "mine", "postBrokerLoginFlowAlias": "my-own-flow"}]
    proc, state = _run(tmp_path, _task("identity providers run the current"), {"providers": providers},
                       {"_keycloak_realm_server_ct": {"stdout": CT},
                        "keycloak_signin": {"aliases": {"post_broker_login": current}}})
    assert proc.returncode == 0, proc.stderr
    updates = [c for c in state["calls"] if c[0] == "update"]
    assert [c[1] for c in updates] == ["identity-provider/instances/bare", "identity-provider/instances/old",
                                       "identity-provider/instances/none"]
    assert all(f"postBrokerLoginFlowAlias={current}" in c for c in updates)
    assert proc.stdout.splitlines() == ["moved: bare", "moved: old", "moved: none"]


def test_the_usage_read_names_the_realm_binding_the_entries_and_the_providers(tmp_path):
    state = {"flows": [{"id": "f1", "alias": "catena-browser", "topLevel": True}],
             "realm": {"browserFlow": "new", "directGrantFlow": "direct grant"},
             "clients": [{"authenticationFlowBindingOverrides": {"browser": "f1"}},
                         {"authenticationFlowBindingOverrides": {}}, {}],
             "providers": [{"postBrokerLoginFlowAlias": "p", "firstBrokerLoginFlowAlias": "q"}, {}]}
    proc, _ = _run(tmp_path, _task("read the sign-in flows and what names them"), state,
                   {"_keycloak_realm_server_ct": {"stdout": CT}})
    assert proc.returncode == 0, proc.stderr
    usage = json.loads(proc.stdout)
    assert usage["flows"] == state["flows"]
    assert sorted(usage["bound"]) == ["direct grant", "f1", "new", "p", "q"]


def test_a_delete_keycloak_refuses_is_kept_and_reported(tmp_path):
    task = _task("delete the sign-in flows nothing names")
    out = []
    for item in ({"id": "old1", "alias": "catena-browser-v0"}, {"id": "old2", "alias": "catena-signin-staff"}):
        proc, state = _run(tmp_path, task, {"refuse": ["old2"]},
                           {"_keycloak_realm_server_ct": {"stdout": CT}, "item": item})
        assert proc.returncode == 0, proc.stderr
        out.append(proc.stdout.strip())
        assert state["calls"][-1][:2] == ["delete", f"authentication/flows/{item['id']}"]
    assert out == ["deleted: catena-browser-v0", "kept: catena-signin-staff: flow in use"]
