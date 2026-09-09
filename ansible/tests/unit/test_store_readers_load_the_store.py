"""A play whose roles read a store-owned value has to load the store.

Phase 3 moves values out of the operator's `.env` and into
/etc/catena/config.json, and the mechanism is one hop: a key declared in
SETTINGS_CONFIG is published by `tasks/load_onbox_config.yml` as a `cfg_*` fact,
and a variable somewhere reads that fact. The hop is invisible at the call site
-- roles say `cloudflare_zone`, not `cfg_cloudflare_zone` -- so moving a key
silently changes which plays can still resolve it.

That is not hypothetical. Moving CLOUDFLARE_ZONE broke exactly one play,
`regenerate-cf-tunnel.yml`, which has no loader by design: it is dispatched from
the panel with the token on the command line. It read the zone from the
inventory and would have started resolving it to nothing -- and the failure
would have been a tunnel regenerated against an empty domain, not an error.

So the rule is checked rather than remembered: if a play's roles read a
store-backed variable, the play loads the store, or it is declared below with
the reason it does not need to.

Run: uv run pytest tests/unit/test_store_readers_load_the_store.py
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

_ANSIBLE = Path(__file__).resolve().parents[2]
_ROLES = _ANSIBLE / "roles"
_PLAYBOOKS = _ANSIBLE / "playbooks"
_GROUP_VARS = _PLAYBOOKS / "group_vars" / "all" / "main.yml"

# Plays that read a store-owned value without the loader, and why that is right.
# Each one has to read the store itself; "it happens to work" is not a reason.
LOADER_EXEMPT = {
    "regenerate-cf-tunnel.yml": (
        "dispatched from the panel with the Cloudflare token on the command "
        "line, so it runs without the loader on purpose. It slurps "
        "/etc/catena/config.json itself -- for the token it rotates and for "
        "the zone it rotates it in -- which is the same file the loader would "
        "have published from"
    ),
}

# The one play that runs BEFORE the store can exist, and the defect that leaves.
#
# bootstrap.yml joins the tailnet on a machine that has nothing on it yet, so
# there is no store to load and no honest way to give it one here. The
# consequence is real and not this gate's to fix: tailnet_provider resolves from
# cfg_tailnet_provider with no inventory fallback, so a Headscale host bootstraps
# against Tailscale SaaS, and only the first site.yml -- which does load the
# store -- puts it right. Recorded in ops/BACKLOG_TECHNICAL.md; the fix is a
# decision about where the store is born, not a line in a playbook.
STORE_PREDATES_THE_PLAY = {
    "bootstrap.yml": (
        "runs against a machine with no /etc/catena yet, so the values it "
        "reads cannot come from a store. See the backlog entry: the tailnet "
        "provider is chosen before anything can record the choice"
    ),
}


def _store_backed_vars() -> dict[str, str]:
    """Variables defined from a `cfg_*` fact, wherever they are defined.

    Same shape as the inventory scan in test_converge_boundary.py, keyed on the
    other source: what the store answers rather than what the .env does.
    """
    out: dict[str, str] = {}
    sources = [_GROUP_VARS] + sorted(_ROLES.glob("*/defaults/main.yml"))
    for src in sources:
        text = src.read_text(encoding="utf-8", errors="ignore")
        for m in re.finditer(r"^([a-z][a-z0-9_]*):((?:.|\n)*?)(?=^\S|\Z)", text, re.M):
            if re.search(r"\bcfg_[a-z0-9_]+", m.group(2)):
                out[m.group(1)] = str(src.relative_to(_ANSIBLE))
    return out


def _roles_of(playbook: Path) -> list[str]:
    plays = yaml.safe_load(playbook.read_text()) or []
    out: list[str] = []
    for play in plays:
        if not isinstance(play, dict):
            continue
        for entry in play.get("roles") or []:
            out.append(entry["role"] if isinstance(entry, dict) else entry)
    return out


def _role_text(role: str) -> str:
    root = _ROLES / role
    if not root.is_dir():
        return ""
    parts = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix not in {".yml", ".yaml", ".j2"}:
            continue
        if path.stat().st_size > 300_000:
            continue
        parts.append(path.read_text(encoding="utf-8", errors="ignore"))
    return "\n".join(parts)


def test_the_scan_finds_the_store_backed_variables():
    """Guards the guard. If the projection stopped being named `cfg_*`, or the
    definitions moved, every assertion below would pass over an empty set."""
    found = _store_backed_vars()
    assert len(found) >= 5, f"only {len(found)} store-backed variables found: {found}"
    assert "cloudflare_zone" in found, (
        "cloudflare_zone is not resolved from the store; it is the value that "
        "made a self-converging host impossible, and this gate exists for it"
    )


def test_every_play_that_reads_the_store_loads_it():
    store_backed = _store_backed_vars()
    offenders: dict[str, list[str]] = {}

    for playbook in sorted(_PLAYBOOKS.glob("*.yml")):
        text = playbook.read_text(encoding="utf-8", errors="ignore")
        if ("load_onbox_config" in text
                or playbook.name in LOADER_EXEMPT
                or playbook.name in STORE_PREDATES_THE_PLAY):
            continue
        reads: set[str] = set()
        for role in _roles_of(playbook):
            body = _role_text(role)
            for var in store_backed:
                if re.search(r"\b" + var + r"\b", body):
                    reads.add(f"{var} (via roles/{role})")
        if reads:
            offenders[playbook.name] = sorted(reads)

    assert not offenders, (
        "these plays read a value the store owns and never load the store, so "
        "the variable resolves to nothing and the play configures the host "
        "against a blank: "
        f"{offenders}. Add the load_onbox_config include, or read the store in "
        "the role and declare the play in LOADER_EXEMPT with the reason"
    )


def test_the_play_that_predates_the_store_is_the_only_one():
    """One play runs before there is a store, and one is the number that keeps
    the rule meaningful. A second entry here would mean a value moved into the
    store that something needs before the store exists, which is a design
    mistake rather than an exception to record."""
    assert set(STORE_PREDATES_THE_PLAY) == {"bootstrap.yml"}, (
        "another play now claims to run before the store exists: "
        f"{sorted(STORE_PREDATES_THE_PLAY)}"
    )
    for name, reason in STORE_PREDATES_THE_PLAY.items():
        assert (_PLAYBOOKS / name).is_file(), f"{name} does not exist"
        assert len(reason) > 60, f"{name} has no real reason: {reason!r}"


def test_every_exemption_reads_the_store_for_itself():
    """An exemption is a promise that the play gets the value another way. A
    play that neither loads the store nor opens it is not exempt, it is
    broken."""
    for name, reason in LOADER_EXEMPT.items():
        playbook = _PLAYBOOKS / name
        assert playbook.is_file(), f"{name} is declared exempt and does not exist"
        assert len(reason) > 60, f"{name} has no real reason: {reason!r}"
        body = playbook.read_text(encoding="utf-8", errors="ignore")
        for role in _roles_of(playbook):
            body += _role_text(role)
        assert "config.json" in body, (
            f"{name} is exempt from loading the store and does not read the "
            "store either, so the values it needs resolve to nothing"
        )
