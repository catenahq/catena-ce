"""The sign-in flows of the Keycloak realm, built under versioned aliases, and
the record that tells a converge whether a person edited them.

reconcile/roles/keycloak/templates/realm-vps.yaml.j2 dumps what
keycloak_signin_flows returns. Every top-level flow Catena owns (catena-browser,
catena-signin-admin, catena-signin-staff, catena-post-broker-login) has the alias
<base>-v<8 hex>; its sub-flows and its authenticator configs embed that alias.
The version is the SHA-256 of the flow's tree and configs with the alias left
out, plus the generation, so it moves only when the flow's steps change or the
generation does. keycloak-config-cli creates a flow under an alias it does not
hold as a new flow and binds the realm's browser flow only after every flow
exists, so a converge that stops half way leaves the realm on the previous
complete version.

Hand edits: after a successful import the converge records, per top-level
alias, a digest of the flow's own record and of its executions as Keycloak
returns them (keycloak_flow_digest), with the generation. The next converge reads the same
listing for each recorded alias; a different digest or a missing flow makes it
build the flows under generation + 1 (keycloak_flow_generation), which are new
aliases the import creates complete before anything is bound to them.
"""

from __future__ import annotations

import hashlib
import json

BROWSER = "catena-browser"
SIGNIN = "catena-signin-"
POST_BROKER = "catena-post-broker-login"

# The tiers an application's own sign-in entry can be limited to, with the tier
# roles each admits.
TIERS = {"admin": ["catena-tier-admin"],
         "staff": ["catena-tier-admin", "catena-tier-staff"]}

# Stands for the alias while a flow is built to be versioned.
_PLACEHOLDER = "@"


def _flow(alias, description, top, executions):
    return {"alias": alias, "description": description, "providerId": "basic-flow",
            "topLevel": top, "builtIn": False, "authenticationExecutions": executions}


def _step(authenticator, requirement, priority, config=None):
    step = {"authenticator": authenticator, "requirement": requirement, "priority": priority}
    if config:
        step["authenticatorConfig"] = config
    return step


def _sub(alias, requirement, priority):
    return {"flowAlias": alias, "requirement": requirement, "priority": priority,
            "authenticatorFlow": True}


def _browser_steps(flow):
    return [
        _step("auth-cookie", "ALTERNATIVE", 10),
        _step("auth-spnego", "DISABLED", 20),
        _step("identity-provider-redirector", "ALTERNATIVE", 25),
        _sub(f"{flow} organization", "ALTERNATIVE", 26),
        _sub(f"{flow} forms", "ALTERNATIVE", 30),
    ]


def _form_flows(flow, password_only):
    """The login-form sub-flows of a browser flow. `forms` runs the credential
    sub-flow (the password or an emailed code, either one) and then the
    conditional second factor; the administrators' flow asks the combined form
    for the password alone."""
    flows = [
        _flow(f"{flow} organization", "Organization identity-first login.", False,
              [_sub(f"{flow} conditional organization", "CONDITIONAL", 10)]),
        _flow(f"{flow} conditional organization", "Whether the organization identity-first login applies.",
              False, [_step("conditional-user-configured", "REQUIRED", 10),
                      _step("organization", "ALTERNATIVE", 20)]),
    ]
    if password_only:
        flows.append(_flow(
            f"{flow} forms", "Username and password, then the second factor.", False,
            [_step("auth-username-password-form", "REQUIRED", 10),
             _sub(f"{flow} conditional 2FA", "CONDITIONAL", 20)]))
    else:
        flows.append(_flow(
            f"{flow} forms", "Username and password or emailed code, then the second factor.", False,
            [_sub(f"{flow} credential", "REQUIRED", 10),
             _sub(f"{flow} conditional 2FA", "CONDITIONAL", 20)]))
        flows.append(_flow(
            f"{flow} credential", "The username and password form, or a code emailed to a client.", False,
            [_step("auth-username-password-form", "ALTERNATIVE", 10),
             _step("catena-client-email-code", "ALTERNATIVE", 20)]))
    flows.append(_flow(
        f"{flow} conditional 2FA", "Whether a second factor is required.", False,
        [_step("conditional-user-configured", "REQUIRED", 10),
         _step("conditional-credential", "REQUIRED", 20, f"{flow}-conditional-credential"),
         _step("auth-otp-form", "ALTERNATIVE", 30),
         _step("webauthn-authenticator", "DISABLED", 40),
         _step("auth-recovery-authn-code-form", "DISABLED", 50)]))
    return flows


def _second_factor_config(flow):
    return {"alias": f"{flow}-conditional-credential", "config": {"credentials": "webauthn-passwordless"}}


def _role_config(alias, role):
    return {"alias": alias, "config": {"condUserRole": role, "negate": "true"}}


def _browser(flow):
    flows = [_flow(flow, "Browser sign-in for applications open to every account.", True,
                   _browser_steps(flow))]
    return flows + _form_flows(flow, False), [_second_factor_config(flow)]


def _tier(flow, tier):
    admitted = TIERS[tier]
    check = [_step("conditional-user-role", "REQUIRED", (i + 1) * 10, f"{flow}-without-{role}")
             for i, role in enumerate(admitted)]
    check.append(_step("catena-tier-refusal", "REQUIRED", (len(admitted) + 1) * 10))
    flows = [
        _flow(flow, f"Browser sign-in for applications open to the {tier} tier.", True,
              [_sub(f"{flow} sign-in", "REQUIRED", 10), _sub(f"{flow} tier check", "CONDITIONAL", 20)]),
        _flow(f"{flow} sign-in", "The steps of the realm's browser flow.", False, _browser_steps(flow)),
        _flow(f"{flow} tier check", "Denies an account holding none of the tier roles this flow admits.",
              False, check),
    ]
    flows += _form_flows(flow, tier == "admin")
    configs = [_role_config(f"{flow}-without-{role}", role) for role in admitted]
    return flows, configs + [_second_factor_config(flow)]


def _post_broker(flow):
    top = [_sub(f"{flow} {tier}", "CONDITIONAL", (i + 1) * 10) for i, tier in enumerate(TIERS)]
    top.append(_step("allow-access-authenticator", "REQUIRED", (len(TIERS) + 1) * 10))
    flows = [_flow(flow, "After a sign-in through an external identity provider: the tier check of the "
                         "application's sign-in flow.", True, top)]
    configs = []
    for tier, admitted in TIERS.items():
        steps = [_step("conditional-client-scope", "REQUIRED", 10, f"{flow}-{tier}-scope")]
        steps += [_step("conditional-user-role", "REQUIRED", (i + 2) * 10, f"{flow}-{tier}-without-{role}")
                  for i, role in enumerate(admitted)]
        steps.append(_step("catena-tier-refusal", "REQUIRED", (len(admitted) + 2) * 10))
        flows.append(_flow(f"{flow} {tier}", f"Denies an account holding none of the tier roles "
                                             f"{SIGNIN}{tier} admits, at an application bound to it.",
                           False, steps))
        configs.append({"alias": f"{flow}-{tier}-scope",
                        "config": {"client_scope": f"{SIGNIN}{tier}", "negate": "false"}})
        configs += [_role_config(f"{flow}-{tier}-without-{role}", role) for role in admitted]
    return flows, configs


def _builders():
    builders = {BROWSER: _browser, POST_BROKER: _post_broker}
    for tier in TIERS:
        builders[SIGNIN + tier] = (lambda flow, tier=tier: _tier(flow, tier))
    return builders


def _version(build, generation):
    flows, configs = build(_PLACEHOLDER)
    canonical = json.dumps({"flows": flows, "configs": configs, "generation": int(generation)},
                           sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:8]


def keycloak_signin_flows(generation):
    """The flows, configs and aliases realm-vps.yaml.j2 declares, built under
    `generation`: {"aliases": {"browser", "admin", "staff", "post_broker_login"},
    "bases": [...], "authenticationFlows": [...], "authenticatorConfig": [...]}."""
    aliases, flows, configs = {}, [], []
    for base, build in _builders().items():
        alias = f"{base}-v{_version(build, generation)}"
        built_flows, built_configs = build(alias)
        aliases[base] = alias
        flows += built_flows
        configs += built_configs
    return {
        "aliases": {"browser": aliases[BROWSER], "admin": aliases[SIGNIN + "admin"],
                    "staff": aliases[SIGNIN + "staff"], "post_broker_login": aliases[POST_BROKER]},
        "bases": list(aliases),
        "authenticationFlows": flows,
        "authenticatorConfig": configs,
    }


_PROJECTED = ("level", "index", "priority", "requirement", "authenticationFlow", "providerId",
              "displayName", "description", "alias")


def keycloak_flow_digest(read):
    """A digest of a flow as the converge reads it: `read` holds "flow", the
    top-level flow's own record (`kcadm get authentication/flows/<id>`), and
    "executions", `kcadm get authentication/flows/<alias>/executions` (every
    level of the flow). The digest covers the record without its id and the
    fields of each execution that a hand edit changes, descriptions included."""
    flow = {key: value for key, value in (read.get("flow") or {}).items() if key != "id"}
    rows = [{key: row.get(key) for key in _PROJECTED} for row in read.get("executions") or []]
    canonical = json.dumps({"flow": flow, "executions": rows}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def keycloak_flow_digests(listing):
    """{alias: digest, or None for a flow Keycloak does not hold} from the lines
    the converge prints: the alias, a tab, the flow read as one line of JSON, or
    `null` for a flow it could not read."""
    digests = {}
    for line in str(listing or "").splitlines():
        alias, _, body = line.partition("\t")
        if not alias:
            continue
        read = json.loads(body) if body else None
        digests[alias] = None if read is None else keycloak_flow_digest(read)
    return digests


def keycloak_flow_generation(record, observed):
    """The generation this converge builds the flows under: the recorded one, or
    the next when a recorded flow reads differently (or not at all) in `observed`
    (keycloak_flow_digests of the recorded aliases). No record is generation 0."""
    if not record:
        return 0
    generation = int(record.get("generation", 0))
    recorded = record.get("flows") or {}
    if any(observed.get(alias) != digest for alias, digest in recorded.items()):
        return generation + 1
    return generation


def keycloak_flow_record(generation, observed):
    """The record the converge writes after a successful import."""
    return {"generation": int(generation), "flows": dict(sorted(observed.items()))}


def keycloak_flow_stale(listing, build):
    """The ids and aliases of the Catena top-level flows no current version
    names that nothing in the realm uses. `listing` is {"flows": kcadm's list of
    id, alias and topLevel, "bound": the aliases and ids the realm, its clients
    and its identity providers name}. A flow in use stays for a later converge."""
    current = set(build["aliases"].values())
    bound = set(listing["bound"])
    stale = []
    for flow in listing["flows"]:
        alias = flow["alias"]
        if not flow.get("topLevel") or alias in current or alias in bound or flow["id"] in bound:
            continue
        if any(alias == base or alias.startswith(base + "-v") for base in build["bases"]):
            stale.append({"id": flow["id"], "alias": alias})
    return stale


class FilterModule:
    def filters(self):
        return {
            "keycloak_signin_flows": keycloak_signin_flows,
            "keycloak_flow_digest": keycloak_flow_digest,
            "keycloak_flow_digests": keycloak_flow_digests,
            "keycloak_flow_generation": keycloak_flow_generation,
            "keycloak_flow_record": keycloak_flow_record,
            "keycloak_flow_stale": keycloak_flow_stale,
        }
