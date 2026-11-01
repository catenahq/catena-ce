"""A service's own address, on a host that has no domain yet.

Every public hostname is `<sub>.{{ cloudflare_zone }}`, so with no domain
entered the zone is empty and the name renders `<sub>.` -- a trailing dot
with nothing after it. A malformed address, and an app that validates its own
hostname refuses to start on it.

Healthchecks is one. `SITE_ROOT=https://<sub>.` normalises to host "<sub>",
which `ALLOWED_HOSTS`' "<sub>." does not match, so Django raises hc.api.E002,
the migrate step exits 1 and the converge cannot finish. A server finishes
installing before its domain is known, which is the order SPEC guarantees.

The gate is the CLASS, not the instances: a compose template that puts a
`*_hostname` into an environment value has to branch on
`catena_public_surface_deferred` and address the service by its network alias
when there is no domain. The two default apps that name themselves
(Healthchecks, Beszel's hub) get their address from the converge, through the
env map of tasks/healthchecks.yml and tasks/beszel.yml, and branch the same way;
keycloak takes the no-fixed-hostname shape for the same reason.

Run: uv run pytest tests/unit/test_no_service_is_addressed_at_a_dangling_hostname.py
"""
from __future__ import annotations

import re
from pathlib import Path

import jinja2
import yaml

ANSIBLE_DIR = Path(__file__).resolve().parents[2]
ROLES_DIR = ANSIBLE_DIR / "reconcile" / "roles"
DEFERRED_FLAG = "catena_public_surface_deferred"

# `{{ foo_hostname }}` -- the product's public-name convention, every one of
# which interpolates the possibly-empty zone.
_HOSTNAME_VAR = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*_hostname)\b")
# A compose environment entry: `SOME_KEY: value`.
_ENV_KEY = re.compile(r"^[A-Z0-9_]+:\s")


def _env_lines_using_a_hostname(text: str) -> list[tuple[str, str]]:
    """(env key, hostname var) for each env value built from a public name."""
    out: list[tuple[str, str]] = []
    for line in text.splitlines():
        bare = line.strip()
        if bare.startswith("#"):
            continue
        if not _ENV_KEY.match(bare):
            continue
        found = _HOSTNAME_VAR.search(bare)
        if found:
            out.append((bare.split(":", 1)[0], found.group(1)))
    return out


def test_every_service_that_names_itself_defers_when_there_is_no_domain() -> None:
    offenders: list[str] = []
    for path in sorted(ROLES_DIR.rglob("*.compose.yml.j2")):
        text = path.read_text(encoding="utf-8")
        uses = _env_lines_using_a_hostname(text)
        if not uses:
            continue
        if DEFERRED_FLAG in text:
            continue
        for key, var in uses:
            offenders.append(
                f"{path.relative_to(ANSIBLE_DIR)}: {key} is built from "
                f"{{{{ {var} }}}}"
            )
    assert not offenders, (
        "these services are handed their own address as `<sub>.` on a host "
        f"with no domain yet, because nothing branches on {DEFERRED_FLAG}. An "
        "app that validates its own hostname refuses to start, and the "
        "converge cannot finish before the domain is entered -- which is the "
        f"order the install has to support: {offenders}"
    )


def _env_map(tasks_file: str, fact: str) -> dict:
    """The values the converge writes into a default app's .env."""
    tasks = yaml.safe_load(
        (ROLES_DIR / "infrastructure" / "tasks" / tasks_file).read_text(encoding="utf-8"))
    return next(t["ansible.builtin.set_fact"][fact] for t in tasks
                if fact in (t.get("ansible.builtin.set_fact") or {}))


def _render(spec: dict, deferred: bool, **values) -> dict:
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.filters["bool"] = lambda v: v is True or str(v).strip().lower() in ("1", "true", "yes", "on")
    return {key: env.from_string(text).render(
                **{DEFERRED_FLAG: deferred}, **values).strip()
            for key, text in spec.items()}


def test_healthchecks_is_reachable_by_its_alias_when_the_domain_is_deferred() -> None:
    """Healthchecks, pinned so the branch cannot be dropped: the address it
    writes into its .env with no domain is built from the network alias, and
    never from the public name."""
    spec = _env_map("healthchecks.yml", "_healthchecks_env")
    values = dict(
        smtp_password="p",
        infrastructure_smtp={"enabled": False, "sender": "a@b.test", "implicit_tls": False},
        healthchecks_network_alias="healthchecks", healthchecks_internal_port=8000,
        healthchecks_hostname="healthchecks.", cloudflare_zone="")
    deferred = _render(spec, True, **values)
    for key in ("SITE_ROOT", "ALLOWED_HOSTS"):
        assert "healthchecks." not in deferred[key], (
            f"the deferred {key} addresses healthchecks at `<sub>.` with an "
            "empty zone, which Django rejects")
        assert "healthchecks" in deferred[key], (
            "the deferred address does not fall back to the network alias every "
            "on-box consumer already dials")
    assert deferred["SITE_ROOT"] == "http://healthchecks:8000"


def test_the_hub_is_reachable_by_its_alias_when_the_domain_is_deferred() -> None:
    spec = _env_map("beszel.yml", "_beszel_hub_env")
    deferred = _render(
        spec, True, beszel_hub_network_alias="beszel-hub", beszel_hub_internal_port=8090,
        beszel_hostname="beszel.", _beszel_gate_subnet="10.0.0.0/24")
    assert deferred["APP_URL"] == "http://beszel-hub:8090"
    served = _render(
        spec, False, beszel_hub_network_alias="beszel-hub", beszel_hub_internal_port=8090,
        beszel_hostname="beszel.acme.test", _beszel_gate_subnet="10.0.0.0/24")
    assert served["APP_URL"] == "https://beszel.acme.test"
