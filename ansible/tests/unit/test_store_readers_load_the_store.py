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
_ROLE_ROOTS = (_ANSIBLE / "bootstrap" / "roles",
               _ANSIBLE / "reconcile" / "roles")

def _role_dir(name: str) -> Path:
    """Where a role lives, whichever side it is on.

    Phase 1b split roles/ into bootstrap/roles/ and reconcile/roles/. Resolved
    by search rather than by a hard-coded side so a role moving across the line
    -- which is a thing this migration does -- does not need this file edited
    too.
    """
    for root in _ROLE_ROOTS:
        if (root / name).is_dir():
            return root / name
    raise AssertionError(f"no role named {name} under {[str(r) for r in _ROLE_ROOTS]}")

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

# Either include satisfies the rule. bootstrap.yml runs the seed half alone --
# the full loader mints secrets and calls the registry, neither of which belongs
# on a machine that does not have Docker yet -- and that half is what publishes
# the cfg_* facts this gate is about.
_LOADERS = ("load_onbox_config", "seed_onbox_config")


def _store_backed_vars() -> dict[str, str]:
    """Variables defined from a `cfg_*` fact, wherever they are defined.

    Same shape as the inventory scan in test_converge_boundary.py, keyed on the
    other source: what the store answers rather than what the .env does.
    """
    out: dict[str, str] = {}
    sources = [_GROUP_VARS] + sorted(
        p for root in _ROLE_ROOTS for p in root.glob("*/defaults/main.yml"))
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
    root = next((r / role for r in _ROLE_ROOTS if (r / role).is_dir()), None)
    if root is None:
        return ""
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
        if (any(loader in text for loader in _LOADERS)
                or playbook.name in LOADER_EXEMPT):
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


def test_bootstrap_seeds_the_store_and_runs_nothing_else():
    """Bootstrap gets the seed half ONLY.

    The rest of load_onbox_config.yml mints the internal service secrets and the
    user-held DR keyset, and asks the container registry which catena-admin
    release to install. Bootstrap runs before Docker exists and before the
    operator has been handed anything, so pulling in the whole loader -- the
    obvious move, since it is one file -- would mint a keyset nobody is there to
    receive and make a network call for an answer nothing can act on.
    """
    # Comments stripped: the play explains why it does NOT run the full loader,
    # and a gate that reads prose would fail on its own explanation.
    text = "\n".join(
        line for line in
        (_PLAYBOOKS / "bootstrap.yml").read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "seed_onbox_config.yml" in text, (
        "bootstrap.yml no longer seeds the store, so bootstrap/roles/tailscale chooses a "
        "control plane from empty facts and a Headscale host takes the "
        "Tailscale fork"
    )
    assert "load_onbox_config" not in text, (
        "bootstrap.yml includes the full loader. That mints the DR keyset and "
        "resolves the catena-admin release from the registry, on a machine with "
        "no Docker and no operator waiting on a keyset. Include "
        "tasks/seed_onbox_config.yml instead"
    )


def test_the_seed_is_the_loaders_own_config_block():
    """One copy of these tasks, not two. The loader includes the same file
    bootstrap does, so a key added to the seed reaches both paths."""
    loader = (_PLAYBOOKS / "tasks" / "load_onbox_config.yml").read_text(encoding="utf-8")
    assert "seed_onbox_config.yml" in loader, (
        "tasks/load_onbox_config.yml no longer includes the seed, so the "
        "converge and bootstrap have separate copies of the config block"
    )
    seed = (_PLAYBOOKS / "tasks" / "seed_onbox_config.yml").read_text(encoding="utf-8")
    for marker in ("settings-config-names", "--set-config", "config-vars",
                   "cfg_cloudflare_zone"):
        assert marker in seed, f"the seed no longer {marker!r}s"
    for forbidden in ("--emit secrets", "catena_admin_release.py", "image-pins"):
        assert forbidden not in seed, (
            f"{forbidden!r} moved into the seed, which bootstrap runs on a "
            "machine with no Docker and no operator waiting on a keyset"
        )


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
