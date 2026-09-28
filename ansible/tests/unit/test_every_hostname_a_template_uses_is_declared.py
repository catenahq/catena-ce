"""Every address a template renders is a variable something declares.

THE FAILURE THIS ENDS. `infrastructure_dash_hostname` and
`catena_admin_hostname` were two names for dash.<zone>. Collapsing them onto
one updated the readers the change knew about and missed
`gatus-infra-spec.json.j2`, which still asked for the deleted name. Ansible
does not resolve a template variable until it renders the file, so nothing
failed until a converge reached that task -- on the bench, at the DAG ROOT,
with 164 scenarios queued behind it:

    'infrastructure_dash_hostname' is undefined
    Origin: reconcile/roles/infrastructure/templates/gatus-infra-spec.json.j2

The sibling test (test_no_variable_is_declared_twice) gates DECLARATIONS, so
it could not see this: the problem was not a second declaration, it was a
reference with none left.

SCOPED TO ADDRESSES, deliberately. A general undefined-variable checker over
every Jinja expression in the tree would need to model loop variables,
registered results, block scope and role defaults precedence -- and would
either drown in false positives or be silently narrowed until it caught
nothing. `*_hostname` and `*_subdomain` are the names the domain work renames,
they are always plain variables rather than loop bindings, and they are the
ones whose absence takes a converge down.

Run: uv run pytest tests/unit/test_every_hostname_a_template_uses_is_declared.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

ANSIBLE_DIR = Path(__file__).resolve().parents[2]
if str(ANSIBLE_DIR) not in sys.path:
    sys.path.insert(0, str(ANSIBLE_DIR))

from helpers import onbox_config  # noqa: E402

GROUP_VARS = ANSIBLE_DIR / "playbooks" / "group_vars" / "all" / "main.yml"
TREES = ("reconcile", "bootstrap", "playbooks")

# Ansible's own. Always defined, never declared by this tree.
_BUILTIN = frozenset({"inventory_hostname", "ansible_hostname"})

# A bare `foo_hostname` / `foo_subdomain` reference inside a Jinja expression.
# Anchored on a word boundary and excluding a leading dot so `item.hostname`
# and `_probe.subdomain` -- attributes of a loop item or a registered result,
# which this test has no business resolving -- are not collected.
_ADDRESS_REF = re.compile(r"(?<![.\w])([a-z][a-z0-9_]*_(?:hostname|subdomain))\b")

# Jinja/Ansible constructs that bind a name locally. A variable introduced by
# one of these is declared, just not in a vars file.
_LOCAL_BIND = re.compile(
    r"(?:\{%-?\s*set\s+|\bfor\s+|loop_var:\s*|\bas\s+)([a-z][a-z0-9_]*)\b")


def _text_files() -> list[Path]:
    out: list[Path] = []
    for tree in TREES:
        base = ANSIBLE_DIR / tree
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if path.is_file() and path.suffix in (".j2", ".yml", ".yaml"):
                out.append(path)
    return sorted(out)


def _declared() -> set[str]:
    """Every address name something in the tree defines.

    Four sources, because a variable is legitimately declared by any of them:
    group_vars, a role's defaults/ or vars/, a `set_fact`, and a `vars:` block
    on a task or an include.
    """
    names: set[str] = set(_BUILTIN)

    gv = yaml.safe_load(GROUP_VARS.read_text(encoding="utf-8")) or {}
    names.update(k for k in gv if _ADDRESS_REF.fullmatch(str(k)))

    # The store projection. load_onbox_config.yml set_facts one `cfg_<name>`
    # per key the panel can write, so those are declared at runtime rather than
    # in any vars file. Read THE MAP, not a prefix: a `cfg_` name the
    # projection does not carry is still a variable nothing defines, which is
    # the whole point.
    names.update(onbox_config.SETTINGS_CONFIG.values())

    for path in _text_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        parts = path.parts
        if "defaults" in parts or "vars" in parts:
            try:
                loaded = yaml.safe_load(text) or {}
            except yaml.YAMLError:
                loaded = {}
            if isinstance(loaded, dict):
                names.update(
                    k for k in loaded if _ADDRESS_REF.fullmatch(str(k)))
        # set_fact / vars: blocks, read as text so a task file's structure does
        # not have to be walked to find them.
        for line in text.splitlines():
            key, _, _ = line.strip().partition(":")
            if _ADDRESS_REF.fullmatch(key):
                names.add(key)
        names.update(_LOCAL_BIND.findall(text))
    return names


def _referenced() -> dict[str, list[str]]:
    """Address name -> the files that render it."""
    out: dict[str, list[str]] = {}
    for path in _text_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        for expr in re.findall(r"\{\{(.*?)\}\}", text, re.DOTALL):
            for name in _ADDRESS_REF.findall(expr):
                out.setdefault(name, []).append(
                    str(path.relative_to(ANSIBLE_DIR)))
    return out


def test_the_collector_finds_the_addresses() -> None:
    """A gate that collects nothing passes for the wrong reason. These two are
    rendered by templates that have carried them for the life of the repo."""
    refs = _referenced()
    for name in ("catena_admin_hostname", "infrastructure_gatus_hostname"):
        assert name in refs, (
            f"{name} is not collected as a template reference, so this gate "
            f"is not reading what it thinks it is"
        )


def test_every_address_a_template_renders_is_declared() -> None:
    declared = _declared()
    orphans: list[str] = []
    for name, files in sorted(_referenced().items()):
        if name in declared:
            continue
        where = sorted(set(files))
        orphans.append(f"{name} (in {', '.join(where)})")
    assert not orphans, (
        "these templates render an address variable nothing declares, so the "
        "converge fails at the task that renders them and nowhere earlier: "
        + "; ".join(orphans)
    )
