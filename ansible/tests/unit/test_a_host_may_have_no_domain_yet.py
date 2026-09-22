"""A server is installed before its domain is decided.

SPEC guarantees a credential-free install: the Cloudflare token that proves a
domain is entered in the dashboard's Settings, afterwards. So at install time
the zone is not merely unknown, it is unknowable -- and the converge used to
ASSERT it, which made the supported order impossible. The install could not
finish, so the panel that takes the domain never came up.

Deferred is not broken, and the difference has to be visible. `cloudflare_zone`
may be empty; `catena_public_surface_deferred` is what every role that would
publish a name reads; and a converge that publishes nothing says so once rather
than issuing a certificate for `dash.` three roles later.

Run: uv run pytest tests/unit/test_a_host_may_have_no_domain_yet.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

ANSIBLE_DIR = Path(__file__).resolve().parents[2]
GROUP_VARS = ANSIBLE_DIR / "playbooks" / "group_vars" / "all" / "main.yml"
SEED = ANSIBLE_DIR / "playbooks" / "tasks" / "seed_onbox_config.yml"
ENV_EXAMPLE = ANSIBLE_DIR / "inventory" / "example" / ".env.example"
PLAYBOOKS = (
    ANSIBLE_DIR / "playbooks" / "converge.yml",
    ANSIBLE_DIR / "playbooks" / "reconcile.yml",
)
RECONCILE_ROLES_DIR = ANSIBLE_DIR / "reconcile" / "roles"
DEFERRED_FLAG = "catena_public_surface_deferred"


def test_the_converge_no_longer_refuses_a_host_with_no_domain() -> None:
    seed = SEED.read_text(encoding="utf-8")
    assert "ansible.builtin.assert" not in seed or "cloudflare_zone" not in seed, (
        "the seed asserts on the zone again. A host cannot acquire a domain "
        "until its panel is up, and its panel does not come up until the "
        "converge finishes, so asserting here makes the supported install "
        "order impossible"
    )


def test_the_deferred_state_is_declared_once_and_derived() -> None:
    """A fact set by one play is absent in every play that does not run it, and
    the roles reading this are reached from several. So it is derived from the
    value it depends on, in the one place that value is declared."""
    gv = yaml.safe_load(GROUP_VARS.read_text(encoding="utf-8")) or {}
    assert "catena_public_surface_deferred" in gv, (
        "nothing declares whether the public surface is deferred, so every "
        "role would have to re-derive it from the zone and they will drift"
    )
    expr = str(gv["catena_public_surface_deferred"])
    assert "cloudflare_zone" in expr, (
        f"the deferred flag does not read the zone it describes: {expr!r}"
    )
    # READING it is the point -- the seed is what announces the deferral. What
    # must not happen is a second DECLARATION, which would be the shadowed-copy
    # problem in a file that runs before group_vars is even consulted.
    seed = SEED.read_text(encoding="utf-8")
    for line in seed.splitlines():
        bare = line.strip()
        if bare.startswith("catena_public_surface_deferred:"):
            raise AssertionError(
                "the seed declares the flag as well as group_vars; one fact, "
                f"one declaration: {bare!r}"
            )


def test_an_empty_zone_resolves_rather_than_failing() -> None:
    """`cfg_cloudflare_zone` is absent on a host whose store holds no domain.
    Without a default the expression raises, which is the assert coming back in
    a worse form -- an undefined-variable error rather than a sentence."""
    gv = yaml.safe_load(GROUP_VARS.read_text(encoding="utf-8")) or {}
    zone = str(gv.get("cloudflare_zone", ""))
    assert "default(" in zone, (
        f"cloudflare_zone has no default, so a store with no domain fails with "
        f"an undefined variable instead of deferring: {zone!r}"
    )


def _roles_the_converge_runs() -> set[str]:
    """Every role name converge.yml or reconcile.yml includes.

    Read from the plays rather than from the roles directory: a role that
    exists but no converge runs -- cloudflare_tunnel_regenerate, driven by
    rotate-tunnel.yml -- may assert whatever it likes, because an operator
    asking to rotate a tunnel has already told it there is a domain.
    """
    names: set[str] = set()
    for path in PLAYBOOKS:
        for play in yaml.safe_load(path.read_text(encoding="utf-8")) or []:
            for entry in (play or {}).get("roles") or []:
                names.add(entry["role"] if isinstance(entry, dict) else entry)
    return names


def _every_task(node) -> list[dict]:
    """Flatten a task file, descending into block / rescue / always."""
    out: list[dict] = []
    if isinstance(node, list):
        for item in node:
            out += _every_task(item)
    elif isinstance(node, dict):
        out.append(node)
        for key in ("block", "rescue", "always"):
            if key in node:
                out += _every_task(node[key])
    return out


def _condition(task: dict) -> str:
    when = task.get("when")
    if isinstance(when, list):
        return " ".join(str(c) for c in when)
    return str(when or "")


def test_no_role_the_converge_runs_asserts_the_zone_unconditionally() -> None:
    """The class of defect, not one instance of it.

    An assert on the zone inside a role the converge runs turns "no domain
    yet" into a converge that cannot finish -- and the failure lands wherever
    that role sits in the play, several roles after the one line that could
    have said the host is waiting. oauth2-proxy did exactly that: every name
    it publishes is built from the domain, so its preflight asserted one, and
    a zone-less host died at the auth layer with a message about secrets.

    Such a role has to DEFER -- announce it and do nothing -- so the gate here
    is the deferral flag in the task's own condition, not the absence of the
    assert.
    """
    offenders: list[str] = []
    for role in sorted(_roles_the_converge_runs()):
        for path in sorted((RECONCILE_ROLES_DIR / role).glob("tasks/*.yml")):
            try:
                loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
            except yaml.YAMLError as e:
                raise AssertionError(f"{path} does not parse: {e}") from e
            for task in _every_task(loaded):
                body = task.get("ansible.builtin.assert") or task.get("assert")
                if not isinstance(body, dict):
                    continue
                that = body.get("that")
                clauses = that if isinstance(that, list) else [that]
                if not any("cloudflare_zone" in str(c) for c in clauses):
                    continue
                if DEFERRED_FLAG not in _condition(task):
                    offenders.append(
                        f"{path.relative_to(ANSIBLE_DIR)}: "
                        f"{task.get('name', '<unnamed>')!r}"
                    )
    assert not offenders, (
        "these tasks refuse a host that has no domain yet, in roles the "
        f"converge runs, without deferring on {DEFERRED_FLAG}: {offenders}"
    )


def test_the_installer_treats_a_blank_domain_as_a_real_answer() -> None:
    """Optionality is inferred from an EMPTY template default (seed.py:
    `is_optional = not default`), so the template is where a key becomes
    optional. A non-empty default here makes the domain mandatory again AND
    re-arms the placeholder problem: `example.com` is a value that converges a
    host believing it serves a domain nobody owns."""
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        if line.startswith("CLOUDFLARE_ZONE="):
            assert line.strip() == "CLOUDFLARE_ZONE=", (
                f"the domain is mandatory again, or carries a placeholder: "
                f"{line!r}"
            )
            return
    raise AssertionError("CLOUDFLARE_ZONE is not in the env template at all")
