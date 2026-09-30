"""A host installs with a tailnet or without one, and 22 closes only on the
panel's Lockdown, after proof.

THE INVARIANT, stated once so it is not filed later as a coverage gap:

    no tailnet + 22 open     every install ends here
    tailnet + 22 open        the host joined the tailnet entered in the panel
    tailnet + 22 closed      the panel's Lockdown joined, proved, and closed
    no tailnet + 22 closed   that Lockdown, after the provider went back to
                             none; applying the chosen access opens 22

ONE INSTALL PATH. Every leg reaches the host over the public SSH address the
install started on, and the install configures no tailnet. Joining one is the
lockdown playbook's first step, which the panel runs on the host. Only the
panel's Lockdown closes the port: key-only SSH is the host's security, and the
tailnet is a convenience whose one security feature is closing 22.

NOTHING CLOSES 22 ON A HOST WITH NO TAILNET. There is nothing to prove, and a
lockdown that closed it would be a lockout with a green checkmark. On such a
host the lockdown opens it instead. These tests are what makes that true
rather than aspirational.

THE SECOND FAILURE MODE is the inverse and just as quiet: a host whose lockdown
already closed 22 must not have it re-opened by a converge. The port is a
public-port registry entry, and the registry's reconciler runs on a timer -- so
the fragment that declares the port is widened only by bootstrap/roles/common's
first write (which refuses to overwrite) and by the lockdown on a host with no
tailnet, and narrowed only by the lockdown after its proof. The reconciler
itself serves the narrowed declaration as open while the tailnet is down
(tests/unit/test_public_ports_ssh_fallback.py), without rewriting it.

Run: uv run pytest tests/unit/test_the_access_plane_has_two_shapes.py
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
COMMON = ANSIBLE / "bootstrap" / "roles" / "common" / "tasks"
LOCKDOWN_TASKS = COMMON / "ufw_lockdown.yml"
LOCKDOWN_PLAY = ANSIBLE / "playbooks" / "lockdown.yml"
CONVERGE = ANSIBLE / "playbooks" / "converge.yml"
BOOTSTRAP = ANSIBLE / "playbooks" / "bootstrap.yml"
GROUP_VARS = ANSIBLE / "playbooks" / "group_vars" / "all" / "main.yml"
TAILSCALE_VALIDATE = (ANSIBLE / "bootstrap" / "roles" / "tailscale" / "tasks"
                      / "validate.yml")


def _load(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _flatten(node) -> list[dict]:
    """Every task, descending into block / rescue / always."""
    out: list[dict] = []
    if isinstance(node, list):
        for item in node:
            out += _flatten(item)
    elif isinstance(node, dict):
        out.append(node)
        for key in ("block", "rescue", "always"):
            if key in node:
                out += _flatten(node[key])
    return out


def _index(tasks: list[dict], needle: str) -> int:
    for i, task in enumerate(tasks):
        if needle.lower() in str(task.get("name", "")).lower():
            return i
    raise AssertionError(
        f"no task matching {needle!r}; the ordering it anchors cannot be checked")


def _conditions(task: dict) -> str:
    when = task.get("when")
    if isinstance(when, list):
        return " ".join(str(c) for c in when)
    return str(when or "")


# --- the method is declared, and it is what everything reads ----------------

@pytest.mark.parametrize("provider,method", [
    ("tailscale", "tailnet"), ("headscale", "tailnet"), (" Headscale ", "tailnet"),
    ("none", "public_ssh"), ("", "public_ssh"), (None, "public_ssh"),
])
def test_the_method_follows_from_the_stored_provider(provider, method):
    """A second derivation of "is this host on a tailnet" is a second answer.
    The store's TAILNET_PROVIDER is the one, because it rides /etc into the
    snapshot and a rebuilt host has to come back with the posture it had. A
    host with nothing stored was installed over SSH alone, and joins nothing."""
    from jinja2 import Environment

    gv = _load(GROUP_VARS) or {}
    expr = str(gv.get("catena_access_method", ""))
    assert "cfg_tailnet_provider" in expr, expr
    context = {} if provider is None else {"cfg_tailnet_provider": provider}
    assert Environment().from_string(expr).render(**context).strip() == method


# --- shape one: no tailnet, 22 open ----------------------------------------

def _runs_tailscale(path: Path) -> list[dict]:
    """Every roles-list entry or include_role task in `path` naming tailscale."""
    found: list[dict] = []
    for play in _load(path) or []:
        for entry in (play or {}).get("roles") or []:
            if isinstance(entry, dict) and entry.get("role") == "tailscale":
                found.append(entry)
        for key in ("pre_tasks", "tasks", "post_tasks"):
            for task in _flatten((play or {}).get(key) or []):
                inc = task.get("ansible.builtin.include_role") or {}
                if isinstance(inc, dict) and inc.get("name") == "tailscale":
                    found.append(task)
    return found


def test_the_tailscale_role_is_skipped_on_a_host_with_no_tailnet():
    """Installing the daemon anyway leaves a node that never authenticates and
    a tailscale0 that never appears, which every later assertion then reads as
    a broken tailnet rather than an absent one."""
    joins = _runs_tailscale(LOCKDOWN_PLAY)
    assert joins, "lockdown.yml does not join the tailnet"
    for entry in joins:
        assert "catena_access_method == 'tailnet'" in _conditions(entry), (
            "lockdown.yml joins the tailnet whatever the access method")


def test_the_tailnet_is_joined_by_the_lockdown_alone():
    """One install path. A join in bootstrap or the converge moves the host
    onto the tailnet mid-install, so the legs after it take a second path, and
    the panel's on-host converge -- which has no tailscale role -- could never
    join a tailnet entered after the install."""
    for path in (CONVERGE, BOOTSTRAP, ANSIBLE / "playbooks" / "reconcile.yml"):
        assert not _runs_tailscale(path), (
            f"{path.name} joins the tailnet; the join belongs to lockdown.yml")


def test_the_lockdown_joins_before_it_closes():
    """The close proves the tailnet path, so the node has to be on it first."""
    tasks = _flatten((_load(LOCKDOWN_PLAY) or [])[0].get("tasks") or [])
    join = _index(tasks, "Join the tailnet")
    close = _index(tasks, "Lock ufw to the declared access method")
    assert join < close, f"join={join} close={close}"


def test_the_tailnet_is_asked_before_anything_closes():
    """The control server's record of the host, and a peer answering through
    the tunnel, are the proof the panel's lockdown has. It runs between the
    join and the ufw change, so a refusal leaves public 22 open; a run that
    only joins closes nothing and proves nothing."""
    tasks = _flatten((_load(LOCKDOWN_PLAY) or [])[0].get("tasks") or [])
    join = _index(tasks, "Join the tailnet")
    prove = _index(tasks, "Prove the tailnet reaches this host")
    close = _index(tasks, "Lock ufw to the declared access method")
    assert join < prove < close, f"join={join} prove={prove} close={close}"
    inc = tasks[prove].get("ansible.builtin.include_role") or {}
    assert (inc.get("name"), inc.get("tasks_from")) == ("tailscale", "reachable.yml")
    assert "catena_lockdown_close_public_ssh" in _conditions(tasks[prove])


def test_no_probe_dials_the_host_from_itself():
    """The panel runs lockdown.yml on the host, where `localhost` is the host:
    a handshake with its own tailnet address passes whatever the tailnet
    thinks. So nothing in the lockdown or the join dials the tailnet address,
    and the reachability check requires a peer."""
    role = ANSIBLE / "bootstrap" / "roles" / "tailscale"
    for path in (LOCKDOWN_TASKS, role / "tasks" / "main.yml"):
        for task in _flatten(_load(path)):
            assert "ansible.builtin.wait_for" not in task, (
                f"{path.name}: {task.get('name')!r} dials an address")
    reachable = (role / "tasks" / "reachable.yml").read_text(encoding="utf-8")
    assert 'TAILNET_REQUIRE_PEER: "1"' in reachable


def test_the_lockdown_writes_nothing_on_a_controller():
    """The panel runs the play on the host, where there is no controller
    inventory to write, and the install does not run it."""
    tasks = _flatten((_load(LOCKDOWN_PLAY) or [])[0].get("tasks") or [])
    assert not [t.get("name") for t in tasks if t.get("delegate_to") == "localhost"]
    cli = (ANSIBLE / "catena_cli.py").read_text(encoding="utf-8")
    assert '"lockdown"' not in cli, "the install runs the lockdown"


def test_validate_gates_inside_the_role_not_by_filtering_the_role_list():
    """playbooks/validate.yml's `validate_roles` is deliberately a literal: the
    audit's Ansible parser resolves validate.yml to its role edges from it,
    statically. A role removed from that list by a condition is a role the
    feature grid stops knowing about, so the role answers for itself."""
    tasks = _flatten(_load(TAILSCALE_VALIDATE))
    assert any("catena_access_method" in _conditions(t) for t in tasks), (
        "the tailscale validation asserts on a daemon a tailnet-free host "
        "deliberately does not run")
    validate = (ANSIBLE / "playbooks" / "validate.yml").read_text(encoding="utf-8")
    assert "tailscale" in validate, (
        "validate.yml no longer names the tailscale role, so the audit's "
        "static parse of validate_roles loses the edge")


def test_a_tailnet_free_host_keeps_its_public_ssh_rule():
    tasks = _flatten(_load(COMMON / "main.yml"))
    allow = tasks[_index(tasks, "Allow public SSH")]
    assert "catena_access_method != 'tailnet'" in _conditions(allow), (
        "the public SSH rule is added only when tailscale0 is absent, so a "
        "host that chose public SSH loses it the moment the interface probe "
        "is answered by something else")


def test_the_lockdown_opens_the_only_way_in_rather_than_closing_it():
    """With no tailnet the lockdown opens 22 and says so. The message is a task,
    not an absence: a lockdown that silently did nothing on this method would
    be indistinguishable from one that ran and failed."""
    tasks = _flatten(_load(LOCKDOWN_TASKS))
    block = tasks[_index(tasks, "open public 22 on a host with no alternative")]
    assert "catena_access_method != 'tailnet'" in _conditions(block)
    assert "catena_lockdown_close_public_ssh" not in _conditions(block)
    said = tasks[_index(tasks, "public 22 is open with no alternative path")]
    msg = str(said.get("ansible.builtin.debug", {}).get("msg", ""))
    assert "is OPEN" in msg, (
        "the message does not state the outcome, so it reads as a step that "
        "was skipped rather than a decision that was taken")
    assert "console" in msg, (
        "the message does not say what the remaining path would be, which is "
        "the whole reason the port stays open")
    assert "chosen access again" in msg, (
        "the message does not say how to reach the other shape, so a client "
        "who wants the port closed is told only that it is not")


def test_a_host_with_no_tailnet_gets_its_public_ssh_back():
    """A Lockdown taken on a tailnet leaves 22 closed and declared private once
    the provider goes back to none. The apply adds the public rule back and
    widens the declaration to `any`, or the reconciler closes the port again on
    its next timer fire."""
    tasks = _flatten(_load(LOCKDOWN_TASKS))
    block = tasks[_index(tasks, "open public 22 on a host with no alternative")]
    inner = block.get("block") or []
    allow = inner[_index(inner, "Allow public SSH")]
    argv = allow.get("ansible.builtin.command", {}).get("argv", [])
    assert argv[:10] == ["ufw", "allow", "from", "any", "to", "any", "port", "22",
                         "proto", "tcp"], argv
    declare = inner[_index(inner, "declare port 22 open")]
    content = str(declare.get("ansible.builtin.copy", {}).get("content", ""))
    assert '"scope": "any"' in content, content
    assert "catena_ssh_fragment_name" in str(
        declare.get("ansible.builtin.copy", {}).get("dest", ""))
    reconcile = inner[_index(inner, "reconcile public ports now")]
    assert reconcile.get("ansible.builtin.systemd_service", {}).get("name") == (
        "catena-public-ports.service")
    assert _index(inner, "Allow public SSH") < _index(inner, "declare port 22 open")


# --- shape two: tailnet, 22 open after the install --------------------------

def _close_block() -> dict:
    tasks = _flatten(_load(LOCKDOWN_TASKS))
    return tasks[_index(tasks, "close public 22 behind the tailnet")]


def test_only_the_panels_lockdown_closes_the_port():
    """The close block runs on catena_lockdown_close_public_ssh alone, which
    lockdown.yml reads from the environment the panel's Lockdown unit carries
    and defaults to false. Nothing else sets it."""
    assert "catena_lockdown_close_public_ssh" in _conditions(_close_block())
    play = (_load(LOCKDOWN_PLAY) or [])[0]
    flag = str((play.get("vars") or {}).get("catena_lockdown_close_public_ssh", ""))
    assert "CATENA_LOCKDOWN_CLOSE_PUBLIC_SSH" in flag, flag
    assert "default('false'" in flag, (
        "the flag must default to false: a caller that forgets it joins and "
        "leaves 22 open, which is the safe side")
    cli = (ANSIBLE / "catena_cli.py").read_text(encoding="utf-8")
    assert "catena_lockdown_close_public_ssh" not in cli, (
        "the installer asks for the close; installs never close public 22")


def test_the_tailnet_rules_and_address_do_not_wait_for_the_close():
    """A run that does not close still joins, adds the tailnet SSH rule and
    learns the tailnet address, with 22 left open."""
    tasks = _flatten(_load(LOCKDOWN_TASKS))
    allow = tasks[_index(tasks, "allow SSH over the tailnet")]
    assert "catena_lockdown_close_public_ssh" not in _conditions(allow)
    names = [t.get("name", "") for t in allow.get("block") or []]
    assert any("Extract tailnet IPv4" in n for n in names)
    assert any("Allow SSH on tailscale0" in n for n in names)


# --- shape three: tailnet, 22 closed after proof ----------------------------

def test_the_close_is_gated_on_the_proof():
    """The sequence that keeps this from being a lockout: add -> PROVE ->
    remove. The proof is reachable.yml, which the play runs before the ufw
    file, and the ufw file only removes the public rule when the play asked to
    close. Detection is not proof: an interface can be up while the route
    through it is dead."""
    tasks = _flatten(_load(LOCKDOWN_TASKS))
    add = _index(tasks, "Allow SSH on tailscale0")
    remove = _index(tasks, "Remove the public SSH allow rule")
    assert add < remove, f"add={add} remove={remove}"
    assert "catena_lockdown_close_public_ssh" in _conditions(_close_block())


def test_the_registry_declaration_narrows_only_after_the_rule_is_removed():
    """The port is a registry entry and the registry's reconciler runs on a
    timer, so the declaration decides the firewall long after this play ends.
    It narrows inside the close block, after the proof and the removal."""
    tasks = _flatten(_load(LOCKDOWN_TASKS))
    remove = _index(tasks, "Remove the public SSH allow rule")
    declare = _index(tasks, "declare port 22 private")
    assert remove < declare
    names = [t.get("name", "") for t in _close_block().get("block") or []]
    assert any("declare port 22 private" in n for n in names)
    body = LOCKDOWN_TASKS.read_text(encoding="utf-8")
    assert '"scope": "private"' in body, (
        "the lockdown declares a scope other than private, so the client's "
        "port document would name a path this host may not have")


def test_the_open_declaration_is_written_once_and_never_rewritten():
    """bootstrap/roles/common runs on every converge. Rewriting the fragment to
    `any` on a host whose lockdown already closed 22 would have the reconciler
    re-open it on the next timer fire -- a lockdown undone by a later converge,
    reported by nothing."""
    tasks = _flatten(_load(COMMON / "main.yml"))
    declare = tasks[_index(tasks, "declare port 22 as open")]
    copy = declare.get("ansible.builtin.copy", {})
    assert copy.get("force") is False, (
        "the open declaration overwrites, so a converge re-opens a locked host")
    assert '"scope": "any"' in str(copy.get("content", "")), (
        "the first declaration is not `any`, which is the state of the host "
        "it describes: a fresh box reached on its public address")


def test_both_writers_name_the_same_fragment():
    """Two files write this declaration, and a disagreement about the filename
    leaves two fragments: one saying the port is open and one saying it is
    private, merged by a reconciler that takes both."""
    gv = _load(GROUP_VARS) or {}
    assert gv.get("catena_ssh_fragment_name"), (
        "the fragment name is not a shared variable, so the two writers each "
        "hold their own spelling of it")
    for path in (COMMON / "main.yml", LOCKDOWN_TASKS):
        body = path.read_text(encoding="utf-8")
        assert "catena_ssh_fragment_name" in body, (
            f"{path.name} spells the fragment name itself")


# --- the lockdown is its own step -------------------------------------------

def test_closing_the_port_is_not_part_of_a_converge():
    """A converge is something a host runs on itself, on a timer. The one step
    that can make it unreachable is a decision taken once -- and re-taking it
    every run re-probes a path nobody asked about, on a host that may have
    since chosen a different method."""
    body = CONVERGE.read_text(encoding="utf-8")
    assert "ufw_lockdown" not in body, (
        "converge.yml still closes the port; a tag-scoped or on-host converge "
        "would then decide the access posture as a side effect")


def test_the_lockdown_play_loads_the_store_before_reading_the_method():
    """The method is a store key. Without the loader it resolves to its
    default on every run, so a host that chose public SSH would have its port
    closed by the fallback rather than by its own answer."""
    play = (_load(LOCKDOWN_PLAY) or [])[0]
    pre = " ".join(str(t) for t in (play.get("pre_tasks") or []))
    assert "load_onbox_config" in pre, (
        "lockdown.yml reads the access method without loading the store")
