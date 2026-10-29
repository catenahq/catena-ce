"""An account that signs in through an external identity provider is refused at
an application outside its tier, as a local account is (CV2).

Keycloak finishes a brokered sign-in in its broker flows and never returns to
the application's browser flow, so the tier check of catena-signin-admin and
catena-signin-staff does not run for it; a provider runs a post-broker-login
flow when it names one, and the realm has no default for it
(IdentityBrokerService finishOrRedirectToPostBrokerLogin, Keycloak 26.6.7).
realm-vps.yaml.j2 declares catena-post-broker-login-v<hex>: per tier, a conditional
sub-flow that holds at an entry carrying the client scope named after the
tier's flow (conditional-client-scope reads the entry's default scopes,
TokenManager getRequestedClientScopes) for an account holding none of the roles
the tier admits, and ends in deny; then an allow step, without which a flow
whose conditions all fail would not succeed (AuthenticationProcessor
authenticateOnly). The extension's binding listener names the flow on every
provider that names none, and realm_bootstrap.yml fails the converge on any
provider still naming none.

The flow is rendered and walked here the way DefaultAuthenticationFlow runs
these steps, against the browser flows' own tier check; the converge's check
of the providers and of the loaded extension run against a stand-in `docker`.

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
from test_realm_signin_flows import ADMITTED, REFUSAL, _aliases

FLOW = _aliases()["catena-post-broker-login"]
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
                assert sub["authenticationExecutions"][-1]["authenticator"] == REFUSAL
                return True
    return False


def _browser_refuses(realm, tier: str, roles: set) -> bool:
    flows, configs = _flows(realm), _configs(realm)
    return _conditions_hold(flows[f"{_aliases()[f'catena-signin-{tier}']} tier check"], configs, set(), roles)


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
        assert steps[-1]["authenticator"] == REFUSAL


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
    json.dump(state, open(path, "w"))
    if kc[:2] == ["get", "serverinfo"]:
        info = {"systemInfo": {}, "providers": state["serverinfo"]}
        if "--fields" in kc:
            # kcadm keeps only the named top-level fields and drops the children
            # of one named without brackets.
            info = {name: ({} if "(" not in kc[kc.index("--fields") + 1] else info[name])
                    for name in kc[kc.index("--fields") + 1].split("(")[0].split(",")}
        print(json.dumps(info))
        sys.exit(0)
    def opt(name):
        return kc[kc.index(name) + 1]
    queries = [kc[i + 1] for i, a in enumerate(kc) if a == "-q"]
    assert kc[:2] == ["get", "identity-provider/instances"], kc
    assert opt("-r") == "vps", kc
    assert "type=USER_AUTHENTICATION" in queries and "realmOnly=true" not in queries, kc
    assert any(q.startswith("max=") and int(q[4:]) > 100 for q in queries), kc
    assert opt("--format") == "csv" and "--noquotes" in kc, kc
    assert opt("--fields") == "alias,postBrokerLoginFlowAlias", kc
    for p in state["providers"]:
        print(p["alias"] + "," + (p.get("post") or ""))
''')

LISTED = {"authenticator": {"providers": {"catena-tier-refusal": {},
                                          "catena-client-email-code": {}}},
          "eventsListener": {"providers": {"catena-tier-binding": {}}}}


def _task(fragment: str) -> dict:
    return next(t for t in yaml.safe_load(BOOTSTRAP.read_text())
                if fragment in str(t.get("name", "")))


def _run(tmp_path: Path, task: dict, state_in: dict, variables: dict):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    docker = bindir / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"calls": [], **state_in}))
    script = Templar(loader=DataLoader(), variables=variables).template(
        trust_as_template(task["ansible.builtin.shell"]))
    proc = subprocess.run(["bash", "-c", script], text=True, capture_output=True,
                          env={"PATH": f"{bindir}:/usr/bin:/bin", "FAKE_KC_STATE": str(state)})
    return proc, json.loads(state.read_text())


def _check(tmp_path: Path, providers: list[dict]):
    return _run(tmp_path, _task(BIND), {"providers": providers},
                {"_keycloak_realm_server_ct": {"stdout": CT}, "keycloak_realm": "vps"})


def test_providers_all_naming_a_flow_pass_and_nothing_is_written(tmp_path):
    proc, state = _check(tmp_path, [
        {"alias": "corp", "post": FLOW},
        {"alias": "partner", "post": "otp-after-broker", "org": "o1"}])
    assert proc.returncode == 0, proc.stderr
    assert all(c[0] == "get" for c in state["calls"])


def test_a_realm_without_providers_passes(tmp_path):
    proc, _ = _check(tmp_path, [])
    assert proc.returncode == 0, proc.stderr


def test_a_provider_naming_no_flow_fails_the_converge_by_name(tmp_path):
    proc, state = _check(tmp_path, [
        {"alias": "google"}, {"alias": "corp", "post": FLOW},
        {"alias": "partner", "post": "", "org": "o1"}])
    assert proc.returncode != 0
    assert proc.stderr.splitlines() == [
        "identity provider google names no post-broker-login flow",
        "identity provider partner names no post-broker-login flow"]
    assert all(c[0] == "get" for c in state["calls"])


def test_the_check_only_reads_and_follows_the_import():
    task = _task(BIND)
    assert task["changed_when"] is False and "update" not in task["ansible.builtin.shell"]
    assert FLOW in _flows(_realm())
    names = [str(t.get("name", "")) for t in yaml.safe_load(BOOTSTRAP.read_text())]
    imported = next(i for i, n in enumerate(names) if "import the realm config" in n)
    login = next(i for i, n in enumerate(names) if "kcadm login to master" in n)
    assert imported < login < names.index(f"Keycloak realm: {BIND}")


# --- the extension is loaded ---------------------------------------------------

LOADED = "the Catena extension is loaded"


def _loaded(tmp_path: Path, serverinfo: dict):
    return _run(tmp_path, _task(LOADED), {"serverinfo": serverinfo},
                {"_kc_master_login": {"stdout": CT}})


def test_a_keycloak_listing_both_providers_passes(tmp_path):
    proc, state = _loaded(tmp_path, LISTED)
    assert proc.returncode == 0, proc.stderr
    assert state["calls"] == [["get", "serverinfo"]]


@pytest.mark.parametrize("spi, provider", [
    ("authenticator", "catena-tier-refusal"),
    ("authenticator", "catena-client-email-code"),
    ("eventsListener", "catena-tier-binding")])
def test_a_keycloak_missing_any_provider_stops_the_converge(tmp_path, spi, provider):
    listed = {name: {"providers": {pid: {} for pid in info["providers"]
                                   if not (name == spi and pid == provider)}}
              for name, info in LISTED.items()}
    proc, _ = _loaded(tmp_path, listed)
    assert proc.returncode != 0
    assert f"{spi}/{provider}" in proc.stderr and "not loaded" in proc.stderr


def test_the_extension_is_checked_before_the_import_that_needs_it():
    names = [str(t.get("name", "")) for t in yaml.safe_load(BOOTSTRAP.read_text())]
    loaded = next(i for i, n in enumerate(names) if LOADED in n)
    login = next(i for i, n in enumerate(names) if "sign kcadm in to the master realm" in n)
    imported = next(i for i, n in enumerate(names) if "import the realm config" in n)
    assert login < loaded < imported


