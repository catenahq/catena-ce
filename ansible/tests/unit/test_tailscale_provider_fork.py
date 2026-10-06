"""bootstrap/roles/tailscale forks on a DECLARED provider, and nothing in the role
re-derives that decision for itself.

Which control plane a host joins is one rule in defaults/main.yml --
``tailnet_provider`` -- that every task guards on, rather than eight separate
tasks each inferring it from whether ``tailnet_control_url`` happens to be
non-empty. A Running node on another control server than that value names is
re-authenticated onto it. The bench drives each fork through the panel
(``ce_install_suite`` the Tailscale one, ``ce_install_headscale`` the Headscale
one, against a real Headscale); these checks hold the fork's shape without a
bench run.

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


def _task(fragment: str) -> dict:
    return next(t for t in _auth_block_tasks() if fragment in t.get("name", ""))


def test_the_headscale_key_is_minted_for_the_users_numeric_id():
    """Headscale's pre-auth key API takes the user as a numeric id; a name in
    that field is a request it cannot parse, so the join never gets a key. The
    id is the one the server lists for the stored name, read before the mint."""
    names = [t.get("name", "") for t in _auth_block_tasks()]
    lookup = _task("list the users the API key can see")
    assert lookup["ansible.builtin.uri"]["url"].endswith("/api/v1/user")
    assert lookup["register"] == "hs_users"
    mint = _task("mint a single-use tagged")
    user = mint["ansible.builtin.uri"]["body"]["user"]
    assert "hs_users.json.users" in user and user.rstrip(" }").endswith(".id"), user
    assert names.index(lookup["name"]) < names.index(mint["name"])
    assert names.index(_task("require the user the key is minted for")["name"]) \
        < names.index(mint["name"])


def test_only_the_tailscale_join_advertises_tags():
    """A Headscale node takes its tags from the pre-auth key, and Headscale
    refuses a node that joins with a pre-auth key and advertises tags."""
    saas = _task("tailscale up with minted key (SaaS)")["ansible.builtin.command"]["argv"]
    hs = _task("Headscale: tailscale up via --login-server")["ansible.builtin.command"]["argv"]
    assert any(a.startswith("--advertise-tags=") for a in saas)
    assert not any(a.startswith("--advertise-tags") for a in hs), hs
    assert "aclTags" in _task("mint a single-use tagged")["ansible.builtin.uri"]["body"]


# --- a provider switch re-authenticates -------------------------------------
def _role_tasks() -> list[dict]:
    return yaml.safe_load(TASKS.read_text())


def _role_task(name: str) -> dict:
    return next(t for t in _role_tasks() if t.get("name") == name)


_MOVED = "Determine whether the stored provider names another control server"


def test_a_running_node_on_another_control_server_is_re_authenticated():
    """Settings moves a joined host between Tailscale and Headscale, or between
    Headscale servers. A Running node is otherwise left alone, so without this
    it stays on the old control server and the join reports success. The
    node's prefs name its control server; it is read before the block."""
    names = [t.get("name") for t in _role_tasks()]
    query = _role_task("Query the control server the node is joined to")
    assert query["ansible.builtin.command"] == "tailscale debug prefs"
    assert query["when"] == "ts_backend_state == 'Running'"
    assert "ControlURL" in _role_task("Read the node's control server")[
        "ansible.builtin.set_fact"]["_ts_control_url"]
    assert names.index(query["name"]) < names.index(_MOVED) < names.index(_AUTH_BLOCK)
    assert "ts_control_moved" in " ".join(_when_clauses(_role_task(_AUTH_BLOCK)))


def test_both_joins_force_the_reauth_on_a_move():
    """`tailscale up` refuses to change the control server of a Running node
    without --force-reauth (tailscale cmd/tailscale/cli/up.go updatePrefs)."""
    want = "--force-reauth={{ 'true' if ts_control_moved | bool else 'false' }}"
    for name in ("tailscale up with minted key (SaaS)",
                 "Headscale: tailscale up via --login-server"):
        assert want in _task(name)["ansible.builtin.command"]["argv"], name


@pytest.mark.parametrize("state, provider, current, moved", [
    ("Running", "headscale", "https://hs.example.net", False),
    ("Running", "headscale", "https://hs-old.example.net", True),
    ("Running", "headscale", "https://controlplane.tailscale.com", True),
    ("Running", "tailscale", "https://controlplane.tailscale.com", False),
    # A name the client counts as Tailscale's own server.
    ("Running", "tailscale", "https://login.tailscale.com", False),
    ("Running", "tailscale", "https://hs.example.net", True),
    # Any other state authenticates anyway, and its prefs are never read.
    ("NeedsLogin", "headscale", None, False),
])
def test_the_move_resolves(state, provider, current, moved):
    """Strictly undefined, as Ansible templates it: a node not Running has no
    control server read, and the expression must not reach for one."""
    from jinja2 import Environment, StrictUndefined

    expr = _role_task(_MOVED)["ansible.builtin.set_fact"]["ts_control_moved"]
    context = {"ts_backend_state": state, "tailnet_provider": provider,
               "tailnet_control_url": "https://hs.example.net",
               "tailscale_saas_control_urls": _defaults()["tailscale_saas_control_urls"]}
    if current is not None:
        context["_ts_control_url"] = current
    got = Environment(undefined=StrictUndefined).from_string(expr).render(**context).strip()
    assert got == str(moved), f"{state} {provider} on {current} resolved to {got}"


def test_the_saas_control_server_is_the_clients_default():
    """`tailscale up` with no --login-server stores ipn.DefaultControlURL."""
    assert _defaults()["tailscale_saas_control_urls"][0] == "https://controlplane.tailscale.com"


# --- the provider is the store's --------------------------------------------
@pytest.mark.parametrize(
    "context,want",
    [
        # The stored choice decides, in BOTH directions -- being unable to go
        # back is the bug the declared choice fixes. A control URL left behind
        # does not make a Tailscale host Headscale.
        ({"cfg_tailnet_provider": "headscale"}, "headscale"),
        ({"cfg_tailnet_provider": "tailscale",
          "tailnet_control_url": "https://hs.example.net"}, "tailscale"),
        # A value that arrived from the settings form with the shape a select
        # submits.
        ({"cfg_tailnet_provider": "  HEADSCALE  "}, "headscale"),
        # Nothing stored is no tailnet: helpers/onbox_config.py settles every
        # store to an explicit provider, and a host installed over SSH alone
        # has none.
        ({}, "none"),
        ({"cfg_tailnet_provider": ""}, "none"),
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
    panel is where these are set, so a second reader here would serve a value
    the client never entered, or one they had already changed."""
    text = DEFAULTS.read_text()
    assert "lookup('dotenv'" not in text, (
        "bootstrap/roles/tailscale/defaults reads the .env directly again"
    )
    d = _defaults()
    assert "cfg_tailnet_control_url" in d["tailnet_control_url"]
    assert "cfg_headscale_user" in d["tailnet_headscale_user"]
    assert "cfg_tailnet_provider" in d["tailnet_provider"]
