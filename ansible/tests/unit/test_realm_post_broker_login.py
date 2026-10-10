"""An account that signs in through an external identity provider is refused at
an application outside its tier, as a local account is (CV2).

Keycloak finishes a brokered sign-in in its broker flows and never returns to
the application's browser flow, so the tier check of catena-signin-admin and
catena-signin-staff does not run for it; a provider runs a post-broker-login
flow when it names one, and the realm has no default for it
(IdentityBrokerService finishOrRedirectToPostBrokerLogin, Keycloak 26.6.7).
realm-vps.yaml.j2 declares catena-post-broker-login: per tier, a conditional
sub-flow that holds at an entry carrying the client scope named after the
tier's flow (conditional-client-scope reads the entry's default scopes,
TokenManager getRequestedClientScopes) for an account holding none of the roles
the tier admits, and ends in deny; then an allow step, without which a flow
whose conditions all fail would not succeed (AuthenticationProcessor
authenticateOnly). realm_bootstrap.yml names the flow on every provider that
names none.

The flow is rendered and walked here the way DefaultAuthenticationFlow runs
these steps, against the browser flows' own tier check; the binding step's
script runs against a stand-in `docker`.

Run: uv run pytest tests/unit/test_realm_post_broker_login.py
"""
from __future__ import annotations

import json
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template

from test_realm_bootstrap_settles import BOOTSTRAP, _render
from test_realm_signin_flows import ADMITTED

FLOW = "catena-post-broker-login"
BIND = "every identity provider runs the tier check after a brokered sign-in"
CT = "keycloak_server.1.abc"
ROLES = ["catena-tier-admin", "catena-tier-staff"]


def _realm() -> dict:
    return yaml.safe_load(_render(realm_seed=False))


def _flows(realm) -> dict[str, dict]:
    return {f["alias"]: f for f in realm["authenticationFlows"]}


def _configs(realm) -> dict[str, dict]:
    return {c["alias"]: c["config"] for c in realm["authenticatorConfig"]}


def _conditions_hold(flow: dict, configs: dict, scopes: set, roles: set) -> bool:
    """Every condition of a conditional sub-flow, as Keycloak evaluates it."""
    held = []
    for e in flow["authenticationExecutions"]:
        config = configs.get(e.get("authenticatorConfig"), {})
        negate = config.get("negate") == "true"
        if e.get("authenticator") == "conditional-client-scope":
            held.append((config["client_scope"] in scopes) != negate)
        elif e.get("authenticator") == "conditional-user-role":
            held.append((config["condUserRole"] in roles) != negate)
    assert held, flow["alias"]
    return all(held)


def _post_broker_refuses(realm, scopes: set, roles: set) -> bool:
    flows, configs = _flows(realm), _configs(realm)
    top = flows[FLOW]["authenticationExecutions"]
    for e in top:
        if e.get("authenticatorFlow"):
            assert e["requirement"] == "CONDITIONAL"
            sub = flows[e["flowAlias"]]
            if _conditions_hold(sub, configs, scopes, roles):
                assert sub["authenticationExecutions"][-1]["authenticator"] == "deny-access-authenticator"
                return True
    return False


def _browser_refuses(realm, tier: str, roles: set) -> bool:
    flows, configs = _flows(realm), _configs(realm)
    return _conditions_hold(flows[f"catena-signin-{tier} tier check"], configs, set(), roles)


# --- the flow ------------------------------------------------------------------

def test_the_flow_is_top_level_and_ends_in_a_step_that_admits():
    flow = _flows(_realm())[FLOW]
    assert flow["topLevel"] is True and flow["builtIn"] is False
    assert flow["providerId"] == "basic-flow"
    steps = flow["authenticationExecutions"]
    assert [(e.get("flowAlias"), e["requirement"]) for e in steps[:-1]] == [
        (f"{FLOW} {tier}", "CONDITIONAL") for tier in ("admin", "staff")]
    assert steps[-1] == {"authenticator": "allow-access-authenticator",
                         "requirement": "REQUIRED", "priority": steps[-1]["priority"]}


def test_each_tier_checks_its_scope_and_its_roles_then_denies():
    realm = _realm()
    flows, configs = _flows(realm), _configs(realm)
    for flow_alias, admitted in ADMITTED.items():
        tier = flow_alias[len("catena-signin-"):]
        steps = flows[f"{FLOW} {tier}"]["authenticationExecutions"]
        assert all(s["requirement"] == "REQUIRED" for s in steps)
        assert steps[0]["authenticator"] == "conditional-client-scope"
        assert configs[steps[0]["authenticatorConfig"]] == {"client_scope": flow_alias,
                                                            "negate": "false"}
        roles = steps[1:-1]
        assert all(s["authenticator"] == "conditional-user-role" for s in roles)
        assert {configs[s["authenticatorConfig"]]["condUserRole"] for s in roles} == admitted
        assert all(configs[s["authenticatorConfig"]]["negate"] == "true" for s in roles)
        assert steps[-1]["authenticator"] == "deny-access-authenticator"


def test_each_tier_has_a_scope_named_after_its_flow_that_carries_no_claim():
    scopes = {s["name"]: s for s in _realm()["clientScopes"]}
    for flow_alias in ADMITTED:
        scope = scopes[flow_alias]
        assert scope["protocol"] == "openid-connect"
        assert "protocolMappers" not in scope
        assert scope["attributes"]["include.in.token.scope"] == "false"
        assert scope["attributes"]["display.on.consent.screen"] == "false"


def test_the_tier_scopes_are_no_default_of_the_realm():
    """Every client the realm makes would carry them, and every brokered
    account outside the tier would be refused everywhere."""
    task = next(t for t in yaml.safe_load(BOOTSTRAP.read_text())
                if "default client scopes hold" in str(t.get("name", "")))
    assert not set(task["vars"]["_keycloak_needed_scopes"]) & set(ADMITTED)
    assert "defaultDefaultClientScopes" not in _realm()


@pytest.mark.parametrize("roles", [set(), {"catena-tier-staff"}, {"catena-tier-admin"},
                                   set(ROLES)], ids=["client", "staff", "admin", "both"])
@pytest.mark.parametrize("tier", ["admin", "staff"])
def test_a_brokered_account_is_refused_where_a_local_one_is(tier, roles):
    realm = _realm()
    scopes = {"profile", "email", "groups", f"catena-signin-{tier}"}
    assert _post_broker_refuses(realm, scopes, roles) is _browser_refuses(realm, tier, roles)


@pytest.mark.parametrize("roles", [set(), set(ROLES)])
def test_an_entry_without_a_tier_scope_admits_every_brokered_account(roles):
    """The realm's own clients, the gates' oauth2-proxy and an application
    open to clients."""
    assert _post_broker_refuses(_realm(), {"profile", "email", "groups"}, roles) is False


def test_a_client_tier_account_is_refused_at_both_tiers():
    realm = _realm()
    for tier in ("admin", "staff"):
        assert _post_broker_refuses(realm, {f"catena-signin-{tier}"}, set()) is True
    assert _post_broker_refuses(realm, {"catena-signin-admin"}, {"catena-tier-staff"}) is True
    assert _post_broker_refuses(realm, {"catena-signin-staff"}, {"catena-tier-staff"}) is False


# --- every provider names it ---------------------------------------------------

FAKE_DOCKER = textwrap.dedent('''\
    #!/usr/bin/env python3
    import json, os, sys
    path = os.environ["FAKE_KC_STATE"]
    state = json.load(open(path))
    args = sys.argv[1:]
    assert args[:3] == ["exec", "keycloak_server.1.abc", "/opt/keycloak/bin/kcadm.sh"], args
    kc = args[3:]
    state["calls"].append(kc)
    def opt(name):
        return kc[kc.index(name) + 1]
    def done(code=0):
        json.dump(state, open(path, "w"))
        sys.exit(code)
    assert opt("-r") == "vps", kc
    if kc[:2] == ["get", "identity-provider/instances"]:
        assert "realmOnly=true" in kc and opt("--format") == "csv" and "--noquotes" in kc, kc
        assert opt("--fields") == "alias,postBrokerLoginFlowAlias", kc
        for p in state["providers"]:
            if not p.get("org"):
                print(p["alias"] + "," + (p.get("post") or ""))
        done()
    if kc[0] == "update" and kc[1].startswith("identity-provider/instances/"):
        alias = kc[1].rsplit("/", 1)[1]
        key, value = opt("-s").split("=", 1)
        assert key == "postBrokerLoginFlowAlias", kc
        next(p for p in state["providers"] if p["alias"] == alias)["post"] = value
        done()
    done(f"unexpected kcadm call {kc}")
''')


def _bind_task() -> dict:
    return next(t for t in yaml.safe_load(BOOTSTRAP.read_text())
                if BIND in str(t.get("name", "")))


def _bind(tmp_path: Path, providers: list[dict]):
    task = _bind_task()
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    docker = bindir / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"providers": providers, "calls": []}))
    variables = {"_keycloak_realm_server_ct": {"stdout": CT}, "keycloak_realm": "vps"}
    script = Templar(loader=DataLoader(), variables=variables).template(
        trust_as_template(task["ansible.builtin.shell"]))
    proc = subprocess.run(["bash", "-c", script], text=True, capture_output=True,
                          env={"PATH": f"{bindir}:/usr/bin:/bin", "FAKE_KC_STATE": str(state)})
    assert proc.returncode == 0, proc.stderr
    variables[task["register"]] = {"stdout": proc.stdout}
    changed = Templar(loader=DataLoader(), variables=variables).template(
        trust_as_template("{{ " + task["changed_when"] + " }}"))
    return proc.stdout, changed, json.loads(state.read_text())


def test_a_provider_naming_no_flow_gets_the_check(tmp_path):
    out, changed, state = _bind(tmp_path, [{"alias": "google"}, {"alias": "corp", "post": ""}])
    assert [p["post"] for p in state["providers"]] == [FLOW, FLOW]
    assert out == "bound: google\nbound: corp\n" and changed


def test_a_flow_named_by_hand_and_an_organizations_provider_are_left_alone(tmp_path):
    providers = [{"alias": "corp", "post": "otp-after-broker"},
                 {"alias": "partner", "org": "o1"}]
    out, changed, state = _bind(tmp_path, [dict(p) for p in providers])
    assert state["providers"] == providers
    assert out == "" and not changed
    assert all(c[0] == "get" for c in state["calls"])


def test_a_realm_without_providers_changes_nothing(tmp_path):
    out, changed, state = _bind(tmp_path, [])
    assert out == "" and not changed


def test_the_binding_names_the_flow_the_realm_declares_after_the_import():
    assert f"flow={FLOW}\n" in _bind_task()["ansible.builtin.shell"]
    assert FLOW in _flows(_realm())
    names = [str(t.get("name", "")) for t in yaml.safe_load(BOOTSTRAP.read_text())]
    imported = next(i for i, n in enumerate(names) if "import the realm config" in n)
    login = next(i for i, n in enumerate(names) if "kcadm login to master" in n)
    assert imported < login < names.index(f"Keycloak realm: {BIND}")
