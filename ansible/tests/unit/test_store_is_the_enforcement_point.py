"""The settings registry is what the converge is allowed to read, enforced.

The registries in helpers/onbox_config.py used to be a description: a list of
keys, accurate because somebody kept it accurate. This gate makes them the
authority.

Three properties, and they are three different failures.

  1. A cfg_* fact a role reads that no registry entry projects is a variable
     that is always undefined. Ansible does not fail on one; the template
     renders empty and the role configures the wrong thing quietly.

  2. A registry entry no role reads is a settings field a client can fill in
     and watch save, that changes nothing. That is worse than a missing field,
     because the page answers "is this configured" with a yes.

  3. A value in STORE_MAY_NOT_DECIDE that is defined from the store is a
     decision that stopped being the operator's. The panel writes the store and
     the converge reads it as root, so anything reachable from the store is
     reachable by whoever controls the panel. For a backup endpoint that is the
     point; for the account the dispatch authenticates as it is a compromise
     that keeps itself.

Run: uv run pytest tests/unit/test_store_is_the_enforcement_point.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_ANSIBLE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ANSIBLE / "helpers"))

import onbox_config as oc  # noqa: E402

sys.path.insert(0, str(_ANSIBLE / "playbooks" / "filter_plugins"))

from image_pin import catena_image_pin  # noqa: E402

_CFG = re.compile(r"\bcfg_[a-z0-9_]+")

# tests/ is excluded on purpose. Several tests name RETIRED projections
# (cfg_smtp_from, cfg_resend_sender_email) precisely to assert they are gone,
# so scanning them would report the absence of a key as the presence of one.
_SKIP_DIRS = {".git", "__pycache__", "collections", "tests", "node_modules"}
_SKIP_SUFFIXES = {".pyc", ".tar.gz", ".png", ".jpg"}
_MAX_BYTES = 300_000


def _tree_files():
    for path in _ANSIBLE.rglob("*"):
        if not path.is_file():
            continue
        if any(part in _SKIP_DIRS or part.startswith(".") for part in path.parts):
            continue
        if path.suffix in _SKIP_SUFFIXES:
            continue
        if path.stat().st_size > _MAX_BYTES:
            continue
        yield path


def _referenced_facts() -> dict[str, set[str]]:
    """Every cfg_* name the converge tree mentions, mapped to where."""
    out: dict[str, set[str]] = {}
    for path in _tree_files():
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for name in _CFG.findall(text):
            out.setdefault(name, set()).add(str(path.relative_to(_ANSIBLE)))
    return out


def test_every_cfg_fact_the_converge_reads_is_declared():
    declared = set(oc.SETTINGS_CONFIG.values())
    undeclared = {k: v for k, v in _referenced_facts().items() if k not in declared}
    assert not undeclared, (
        "these cfg_* variables are read but no SETTINGS_CONFIG entry projects "
        "them, so they are always undefined and the role renders an empty "
        f"value without failing: {undeclared}"
    )


def test_every_declared_projection_is_read():
    referenced = set(_referenced_facts())
    unread = sorted(set(oc.SETTINGS_CONFIG.values()) - referenced)
    assert not unread, (
        "these settings keys project a fact nothing reads, so a client can set "
        "them, watch them save, and change nothing on the host: "
        f"{unread}"
    )


def _definition(var: str) -> tuple[Path, str] | None:
    """The value block for `var` in whichever defaults file defines it.

    Line-based rather than a YAML parse: what matters is the literal text of
    the definition, including the folded scalars and Jinja these files are
    written in, and a parse would resolve exactly the thing being inspected.
    """
    # Both role roots since phase 1b. A scan of one of them would call every
    # variable on the other side undefined, and this gate reads that as a
    # protected name nothing defines.
    for path in sorted(_ANSIBLE.glob("*/roles/*/defaults/main.yml")):
        lines = path.read_text(encoding="utf-8").split("\n")
        for i, line in enumerate(lines):
            if not line.startswith(var + ":"):
                continue
            block = [line]
            for nxt in lines[i + 1:]:
                if nxt and not nxt[0].isspace():
                    break
                block.append(nxt)
            return path, "\n".join(block)
    return None


def test_every_protected_variable_actually_exists():
    """A guard over a name nothing defines protects nothing, and reads as
    coverage. A rename has to fail here rather than silently empty the set."""
    missing = [v for v in oc.STORE_MAY_NOT_DECIDE if _definition(v) is None]
    assert not missing, (
        "STORE_MAY_NOT_DECIDE names variables no role defines; they were "
        f"renamed or removed and the guard over them is now inert: {missing}"
    )


def test_the_store_cannot_decide_the_trust_path():
    offenders = {}
    for var, why in oc.STORE_MAY_NOT_DECIDE.items():
        found = _definition(var)
        if found is None:
            continue
        path, block = found
        hits = sorted(set(_CFG.findall(block)))
        if hits:
            offenders[var] = (str(path.relative_to(_ANSIBLE)), hits, why)
    assert not offenders, (
        "these values are defined from the store, and the panel writes the "
        "store: whoever controls the panel now controls "
        f"{offenders}"
    )


def test_a_pin_naming_another_repository_is_ignored():
    """The one store-driven exception, and the limit on it.

    A pin MAY move a service's version. It may never move its repository, or a
    store key would be able to point a service at any image on earth, which is
    the same compromise the trust-path guard above exists to prevent.
    """
    floor = "ghcr.io/catenahq/catena-admin:v1.0.0"
    assert catena_image_pin(
        floor, {"ghcr.io/catenahq/catena-admin": "docker.io/attacker/x:v9.9.9"}
    ) == floor
    # And the permitted half still works, or the guard above would be passing
    # for the wrong reason.
    assert catena_image_pin(
        floor, {"ghcr.io/catenahq/catena-admin": "ghcr.io/catenahq/catena-admin:v1.1.0"}
    ) == "ghcr.io/catenahq/catena-admin:v1.1.0"
