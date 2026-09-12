"""bootstrap/roles/tailscale forks on a DECLARED provider, and nothing in the role
re-derives that decision for itself.

Which control plane a host joins is one rule in defaults/main.yml --
``tailnet_provider`` -- that every task guards on, rather than eight separate
tasks each inferring it from whether ``tailnet_control_url`` happens to be
non-empty. This file exists because the role has no other coverage at all: the
bench's
``ce_install_headscale`` scenario drives ``tailscale up --login-server``
directly (a hand-written copy of what the role emits, validating the JOIN
CONTRACT against a real Headscale), so it would stay green through any change
to the fork itself. The Tailscale side is exercised by every ce_install run;
the Headscale side of the ROLE is exercised by nothing.

What a wrong fork costs is the reason this is a test and not a comment: the
node joins the wrong control plane, or joins none, and administrative access to
the host runs over that network.

Run: uv run pytest tests/unit/test_tailscale_provider_fork.py
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROLE = Path(__file__).resolve().parents[3] / "ansible" / "bootstrap" / "roles" / "tailscale"
TASKS = ROLE / "tasks" / "main.yml"
DEFAULTS = ROLE / "defaults" / "main.yml"

# The name of the block whose tasks are the fork. Everything before it installs
# the package and reads the current state, which is provider-independent.
_AUTH_BLOCK = "Authenticate the node"


def _defaults() -> dict:
    return yaml.safe_load(DEFAULTS.read_text())


def _auth_block_tasks() -> list[dict]:
    """The tasks inside the Authenticate-the-node block."""
    for task in yaml.safe_load(TASKS.read_text()):
        if task.get("name") == _AUTH_BLOCK:
            return task["block"]
    raise AssertionError(f"{TASKS} no longer has a {_AUTH_BLOCK!r} block")


def _when_clauses(task: dict) -> list[str]:
    """A task's `when` as a flat list of strings. Ansible accepts either a
    scalar or a list, and a change from one to the other must not make this
    assertion vacuous."""
    when = task.get("when")
    if when is None:
        return []
    if isinstance(when, str):
        return [when]
    return [str(w) for w in when]


# --- the fork itself --------------------------------------------------------
def test_every_fork_task_guards_on_the_declared_provider():
    """A task in this block with no provider guard runs on BOTH paths. The
    Tailscale OAuth exchange running on a Headscale host fails the converge on
    a credential that host correctly does not have."""
    unguarded = []
    for task in _auth_block_tasks():
        clauses = " ".join(_when_clauses(task))
        if "tailnet_provider" not in clauses:
            unguarded.append(task.get("name", "<unnamed>"))
    assert not unguarded, (
        "tasks in the authenticate block run on both control planes: "
        f"{unguarded}"
    )


def test_no_task_re_derives_the_provider_from_the_control_url():
    """The derivation is the FALLBACK in defaults/main.yml, for a host that has
    never stored a choice. A task repeating it is a second rule that can
    disagree with the first -- which is what this change removed."""
    offenders = []
    for task in _auth_block_tasks():
        for clause in _when_clauses(task):
            if "tailnet_control_url" in clause and "length" in clause:
                offenders.append((task.get("name", "<unnamed>"), clause))
    # The one permitted mention is the assert that a Headscale host HAS an
    # address -- it checks the value, it does not decide the fork.
    offenders = [
        (name, clause) for name, clause in offenders
        if "require a control server address" not in name
    ]
    assert not offenders, f"the control-URL inference is back on a task: {offenders}"


def test_both_forks_are_still_reachable():
    """Guarding every task on one value is only correct if both values are
    used. A typo in one guard would leave a control plane with no tasks at
    all, and the role would report success having joined nothing."""
    guarded = {"tailscale": 0, "headscale": 0}
    for task in _auth_block_tasks():
        clauses = " ".join(_when_clauses(task))
        for provider in guarded:
            if f"'{provider}'" in clauses:
                guarded[provider] += 1
    for provider, count in guarded.items():
        assert count, f"no task in the authenticate block runs for {provider}"


def test_headscale_requires_an_address_before_it_is_used():
    """Fails at the top of the fork naming the setting, rather than partway
    through against an empty URL -- a POST to `/api/v1/preauthkey` on an empty
    base URL is a connection error, not an answer."""
    names = [t.get("name", "") for t in _auth_block_tasks()]
    guard = next((i for i, n in enumerate(names)
                  if "require a control server address" in n), None)
    assert guard is not None, (
        "nothing asserts that a Headscale host has a control server address"
    )
    mint = next(i for i, n in enumerate(names) if "mint a single-use tagged" in n)
    assert guard < mint, "the address assert runs after the API call that needs it"


# --- the fallback rule ------------------------------------------------------
@pytest.mark.parametrize(
    "context,want",
    [
        # Nothing stored, no control URL: a Tailscale SaaS host, which is
        # every host that has never touched the setting.
        ({}, "tailscale"),
        # Nothing stored, a control URL: the rule that decided it before the
        # field existed. Every already-installed Headscale host is this case,
        # and getting it wrong moves them onto Tailscale on the next converge.
        ({"tailnet_control_url": "https://hs.example.net"}, "headscale"),
        # An explicit choice outranks the inference, in BOTH directions --
        # being unable to go back is the bug the declared choice fixes.
        ({"cfg_tailnet_provider": "headscale"}, "headscale"),
        ({"cfg_tailnet_provider": "tailscale",
          "tailnet_control_url": "https://hs.example.net"}, "tailscale"),
        # A value that arrived from the settings form with the shape a select
        # submits, and one that did not.
        ({"cfg_tailnet_provider": "  HEADSCALE  "}, "headscale"),
        ({"cfg_tailnet_provider": ""}, "tailscale"),
    ],
)
def test_the_provider_default_resolves(context, want):
    from jinja2 import Environment

    expr = _defaults()["tailnet_provider"]
    got = Environment().from_string(expr).render(**context).strip()
    assert got == want, f"{context} resolved to {got!r}, want {want!r}"


def test_the_store_is_the_only_reader_of_the_three_values():
    """defaults/main.yml reads the store's projected facts and nothing else.
    A `lookup('dotenv', ...)` back here is the silent-fallback bug class: the
    .env seeds the store on the first converge and is never read again, so a
    second reader here would serve a stale value to a host whose client had
    already changed the setting in the panel."""
    text = DEFAULTS.read_text()
    assert "lookup('dotenv'" not in text, (
        "bootstrap/roles/tailscale/defaults reads the .env directly again"
    )
    d = _defaults()
    assert "cfg_tailnet_control_url" in d["tailnet_control_url"]
    assert "cfg_headscale_user" in d["tailnet_headscale_user"]
    assert "cfg_tailnet_provider" in d["tailnet_provider"]
