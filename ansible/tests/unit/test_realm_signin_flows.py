"""An application's own sign-in button turns away an account outside the tiers
its flow admits.

realm-vps.yaml.j2 declares two browser flows, catena-signin-admin and
catena-signin-staff, which dashboard-sync binds an application's own sign-in
entry to by tier. Each runs every step of the realm's built-in browser flow in a
required sub-flow (as Keycloak 26.6 builds it: cookie, Kerberos, identity
provider redirector, the organization identity-first login, the login form with
its conditional second factor), then a conditional sub-flow whose last step
denies an account holding none of the tier roles the flow admits. The tier roles
come from the /admin and /staff groups.

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

from pathlib import Path

import yaml

from test_realm_bootstrap_settles import BOOTSTRAP, _render

ANSIBLE = Path(__file__).resolve().parents[2]
ENV_TEMPLATE = (ANSIBLE / "reconcile" / "roles" / "infrastructure" / "templates"
                / "dashboard-sync.env.j2")

ADMITTED = {
    "catena-signin-admin": {"catena-tier-admin"},
    "catena-signin-staff": {"catena-tier-admin", "catena-tier-staff"},
}

# The realm's built-in browser flow on a Keycloak 26.6 host, step by step:
# (sub-flow, authenticator or nested sub-flow, requirement).
BROWSER_STEPS = [
    ("sign-in", "auth-cookie", "ALTERNATIVE"),
    ("sign-in", "auth-spnego", "DISABLED"),
    ("sign-in", "identity-provider-redirector", "ALTERNATIVE"),
    ("sign-in", "@organization", "ALTERNATIVE"),
    ("sign-in", "@forms", "ALTERNATIVE"),
    ("organization", "@conditional organization", "CONDITIONAL"),
    ("conditional organization", "conditional-user-configured", "REQUIRED"),
    ("conditional organization", "organization", "ALTERNATIVE"),
    ("forms", "auth-username-password-form", "REQUIRED"),
    ("forms", "@conditional 2FA", "CONDITIONAL"),
    ("conditional 2FA", "conditional-user-configured", "REQUIRED"),
    ("conditional 2FA", "conditional-credential", "REQUIRED"),
    ("conditional 2FA", "auth-otp-form", "ALTERNATIVE"),
    ("conditional 2FA", "webauthn-authenticator", "DISABLED"),
    ("conditional 2FA", "auth-recovery-authn-code-form", "DISABLED"),
]


def _realm() -> dict:
    return yaml.safe_load(_render(realm_seed=False))


def _flows() -> dict[str, dict]:
    return {f["alias"]: f for f in _realm()["authenticationFlows"]}


def _step(flow: str, execution: dict) -> str:
    sub = execution.get("flowAlias")
    return "@" + sub[len(flow) + 1:] if sub else execution["authenticator"]


def test_both_flows_are_top_level_and_bound_by_their_aliases():
    flows = _flows()
    for alias in ADMITTED:
        assert flows[alias]["topLevel"] is True
        assert flows[alias]["builtIn"] is False
        assert flows[alias]["providerId"] == "basic-flow"
    assert all(not f["builtIn"] for f in flows.values())


def test_each_copy_keeps_every_step_of_the_browser_flow():
    flows = _flows()
    for alias in ADMITTED:
        got = []
        for sub in ("sign-in", "organization", "conditional organization", "forms",
                    "conditional 2FA"):
            for e in flows[f"{alias} {sub}"]["authenticationExecutions"]:
                got.append((sub, _step(alias, e), e["requirement"]))
        assert got == BROWSER_STEPS, alias


def test_the_second_factor_condition_matches_the_realm_flow():
    realm = _realm()
    configs = {c["alias"]: c["config"] for c in realm["authenticatorConfig"]}
    for alias in ADMITTED:
        [cond] = [e for e in _flows()[f"{alias} conditional 2FA"]["authenticationExecutions"]
                  if e["authenticator"] == "conditional-credential"]
        assert configs[cond["authenticatorConfig"]] == {"credentials": "webauthn-passwordless"}


def test_the_sign_in_steps_run_first_and_the_deny_step_last():
    flows = _flows()
    for alias in ADMITTED:
        top = flows[alias]["authenticationExecutions"]
        assert [(e["flowAlias"], e["requirement"]) for e in top] == [
            (f"{alias} sign-in", "REQUIRED"), (f"{alias} tier check", "CONDITIONAL")]
        steps = flows[f"{alias} tier check"]["authenticationExecutions"]
        assert steps[-1]["authenticator"] == "deny-access-authenticator"
        assert all(s["requirement"] == "REQUIRED" for s in steps)


def test_the_deny_step_runs_only_for_an_account_without_every_admitted_role():
    realm = _realm()
    configs = {c["alias"]: c["config"] for c in realm["authenticatorConfig"]}
    for alias, admitted in ADMITTED.items():
        conditions = _flows()[f"{alias} tier check"]["authenticationExecutions"][:-1]
        assert all(c["authenticator"] == "conditional-user-role" for c in conditions)
        roles = {configs[c["authenticatorConfig"]]["condUserRole"] for c in conditions}
        assert roles == admitted, alias
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
    referenced = [e["flowAlias"] for f in realm["authenticationFlows"]
                  for e in f["authenticationExecutions"] if e.get("authenticatorFlow")]
    assert sorted(referenced) == sorted(a for a in aliases if a not in ADMITTED)


def test_the_converge_reads_both_flow_ids_and_fails_without_them():
    tasks = [t for t in yaml.safe_load(BOOTSTRAP.read_text()) if isinstance(t, dict)]
    names = [str(t.get("name", "")) for t in tasks]
    read = next(i for i, n in enumerate(names) if "read the sign-in flow ids" in n)
    keep = next(i for i, n in enumerate(names) if "keep the sign-in flow ids" in n)
    check = next(i for i, n in enumerate(names) if "both sign-in flows exist" in n)
    imported = next(i for i, n in enumerate(names) if "import the realm config" in n)
    assert imported < read < keep < check
    argv = tasks[read]["ansible.builtin.command"]["argv"]
    assert argv[argv.index("get") + 1] == "authentication/flows"
    kept = tasks[keep]["ansible.builtin.set_fact"]["keycloak_signin_flows"]
    assert set(kept) == {"admin", "staff"}
    for tier in kept:
        assert f"catena-signin-{tier}" in kept[tier]
    assert tasks[check]["ansible.builtin.assert"]["that"] == [
        "keycloak_signin_flows.admin | length > 0",
        "keycloak_signin_flows.staff | length > 0"]


def test_dashboard_sync_gets_the_flow_ids():
    lines = [ln for ln in ENV_TEMPLATE.read_text().splitlines()
             if ln.startswith("KEYCLOAK_SIGNIN_FLOWS=")]
    assert lines == ["KEYCLOAK_SIGNIN_FLOWS='{{ keycloak_signin_flows | default({}) | to_json }}'"]
