"""A fact has one declaration, or the second one is unreachable.

`playbooks/group_vars/` is playbook-adjacent, so it loads on the operator
path AND on the on-host reconcile. Role defaults are Ansible's lowest
precedence. Wherever both name the same variable, the role's copy can
never apply -- and it still reads as the authority to whoever opens that
file, so an edit to it silently does nothing.

Three were found this way, all in reconcile/roles/infrastructure:
`healthchecks_subdomain` and `healthchecks_hostname`, which agreed with
group_vars and so hid harmlessly, and `ansible_managed`, which did NOT.
group_vars said "Managed by catena -- do not edit directly; regenerate
via the installer / converge.yml." while the role said "Managed by catena
- do not edit directly." Every file that role templates carried the
group_vars banner; the role's own text had never been written to a host.

The same shape cost the panel its single name: `catena_admin_hostname`
and `infrastructure_dash_hostname` both rendered dash.<zone> from their
own subdomain variable, so they agreed and nothing noticed -- until
something changed one.
"""
from __future__ import annotations

from pathlib import Path

import yaml

ANSIBLE_DIR = Path(__file__).resolve().parents[2]
GROUP_VARS = ANSIBLE_DIR / "playbooks" / "group_vars" / "all" / "main.yml"


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text()) or {}


def _role_defaults() -> list[Path]:
    """Reconcile roles only.

    The bootstrap tree has twelve more collisions of the same mechanical
    shape (`common` 4, `storage` 5, `tailscale` 3), and they are NOT the
    same finding. Those are inventory-owned values -- the block device,
    the ops keys, the tailnet tags -- that group_vars renders from
    `lookup('dotenv', ...)` and the role declares as a standalone
    fallback. Deleting them is a judgement about bootstrap's ownership
    model, on the side of the boundary where "if this is wrong, is
    anything left that can fix it" answers no. Raised separately rather
    than settled by a test written for a different question.
    """
    return sorted((ANSIBLE_DIR / "reconcile" / "roles").glob("*/defaults/main.yml"))


def test_group_vars_shares_no_key_with_any_role_default() -> None:
    group_vars = set(_load(GROUP_VARS))
    assert group_vars, "group_vars/all/main.yml parsed empty; the check is vacuous"

    collisions: list[str] = []
    for path in _role_defaults():
        role = path.parts[-3]
        for key in sorted(group_vars & set(_load(path))):
            collisions.append(f"{role}/defaults/main.yml declares {key}")

    assert not collisions, (
        "group_vars/all/main.yml already declares these, and group_vars wins, "
        "so the role's copy can never apply:\n  "
        + "\n  ".join(collisions)
        + "\nDelete the role's declaration. A second copy that cannot take "
          "effect is worse than no copy: it invites an edit that does nothing."
    )


def test_the_panel_hostname_has_exactly_one_name() -> None:
    """`infrastructure_dash_hostname` was a second name for
    `catena_admin_hostname`. oauth2_proxy and the trust path read one; the
    DR keyset banner and validate's gated-host probe set read the other.
    Both resolved to dash.<zone>, so a rename would have moved half of
    them and left the banner printing a URL that does not resolve."""
    text = GROUP_VARS.read_text()
    assert "catena_admin_hostname:" in text, "the panel lost its hostname"
    assert "infrastructure_dash_hostname" not in text, (
        "infrastructure_dash_hostname is back; it is a second name for "
        "catena_admin_hostname and the two cannot be kept in step by hand"
    )
