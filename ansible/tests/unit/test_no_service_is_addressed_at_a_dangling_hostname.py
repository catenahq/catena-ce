"""A service's own address, on a host that has no domain yet.

Every public hostname is `<sub>.{{ cloudflare_zone }}`, so with no domain
entered the zone is empty and the name renders `heartbeat.` -- a trailing dot
with nothing after it. That is not a degraded address, it is a malformed one,
and an app that validates its own hostname refuses to start on it.

Healthchecks proved it. `SITE_ROOT=https://heartbeat.` normalises to host
"heartbeat" while `ALLOWED_HOSTS` carried "heartbeat.", so Django raised
hc.api.E002, the migrate step exited 1, the FATAL hook stopped the container
on 137 and the whole converge returned rc=2. A server could not finish
installing before its domain was known, which is the exact order SPEC
guarantees. Run eecd's `install_without_a_domain` failed at
stage-1-converge-with-no-domain on it.

The gate is the CLASS, not the two instances: a compose template that puts a
`*_hostname` into an environment value has to branch on
`catena_public_surface_deferred` and address the service by its network alias
when there is no domain. Both known consumers (healthchecks, beszel-hub) do;
keycloak takes the no-fixed-hostname shape for the same reason.

Run: uv run pytest tests/unit/test_no_service_is_addressed_at_a_dangling_hostname.py
"""
from __future__ import annotations

import re
from pathlib import Path

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


def test_healthchecks_is_reachable_by_its_alias_when_the_domain_is_deferred() -> None:
    """The instance that cost a converge, pinned so the branch cannot be
    dropped while the class gate above still passes on the file's other half."""
    text = (
        ROLES_DIR / "infrastructure" / "templates" / "healthchecks.compose.yml.j2"
    ).read_text(encoding="utf-8")

    deferred = text.split(f"{{% if {DEFERRED_FLAG}", 1)
    assert len(deferred) > 1, "healthchecks no longer branches on the flag"

    for key in ("SITE_ROOT", "ALLOWED_HOSTS"):
        assert f"{key}:" in text, f"{key} is gone from the template"

    # The deferred SITE_ROOT must not be built from the public name.
    for chunk in re.findall(
        r"\{%\s*if\s+" + DEFERRED_FLAG + r".*?\{%\s*else\s*%\}",
        text,
        re.S,
    ):
        assert "healthchecks_hostname" not in chunk, (
            "the deferred branch still addresses healthchecks at "
            "`heartbeat.<empty zone>`, which is what Django rejected"
        )
        assert "healthchecks_network_alias" in chunk, (
            "the deferred branch does not fall back to the network alias every "
            "on-box consumer already dials"
        )
