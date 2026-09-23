"""A host installs with a tailnet or without one, and 22 closes only on proof.

THE INVARIANT, stated once so it is not filed later as a coverage gap. Two
combinations are reachable and only two:

    tailnet + 22 closed      the path every host took until now
    no tailnet + 22 open     a host with no alternative keeps the one it has

`no tailnet + 22 closed` is unreachable BY CONSTRUCTION. There is nothing to
prove, so nothing may close the port, and a lockdown that could reach that state
would be a lockout with a green checkmark. These tests are what makes "by
construction" true rather than aspirational.

THE SECOND FAILURE MODE is the inverse and just as quiet: a host whose lockdown
already closed 22 must not have it re-opened. The port is a public-port registry
entry, and the registry's reconciler runs on a timer -- so the fragment that
declares the port has exactly one widening writer (bootstrap/roles/common, which
refuses to overwrite) and exactly one narrowing writer (the lockdown, after its
proof).

Run: uv run pytest tests/unit/test_the_access_plane_has_two_shapes.py
"""
from __future__ import annotations

from pathlib import Path

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

def test_the_method_is_one_variable_read_from_the_store():
    """A second derivation of "is this host on a tailnet" is a second answer.
    The store's key is the one, because it rides /etc into the snapshot and a
    rebuilt host has to come back with the posture it had."""
    gv = _load(GROUP_VARS) or {}
    expr = str(gv.get("catena_access_method", ""))
    assert "cfg_access_method" in expr, (
        "the access method is not read from the store projection; a host's "
        f"declared posture would not survive a rebuild: {expr!r}")
    assert "tailnet" in expr, (
        "the fallback is not the tailnet, so a host that stored nothing would "
        "have its firewall decided by the wrong default")


# --- shape one: no tailnet, 22 open ----------------------------------------

def test_the_tailscale_role_is_skipped_on_a_host_with_no_tailnet():
    """Installing the daemon anyway leaves a node that never authenticates and
    a tailscale0 that never appears, which every later assertion then reads as
    a broken tailnet rather than an absent one."""
    for path in (CONVERGE, BOOTSTRAP):
        gated = False
        for play in _load(path) or []:
            for entry in (play or {}).get("roles") or []:
                if isinstance(entry, dict) and entry.get("role") == "tailscale":
                    assert "catena_access_method" in _conditions(entry), (
                        f"{path.name}: the tailscale role runs unconditionally")
                    gated = True
        assert gated, f"{path.name} does not run the tailscale role at all"


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


def test_the_lockdown_refuses_rather_than_closing_the_only_way_in():
    """The unreachable third combination. The refusal is a task with a message,
    not an absence: a lockdown that silently did nothing on this method would
    be indistinguishable from one that ran and failed."""
    tasks = _flatten(_load(LOCKDOWN_TASKS))
    refusal = tasks[_index(tasks, "refuse to close port 22")]
    assert "catena_access_method != 'tailnet'" in _conditions(refusal)
    msg = str(refusal.get("ansible.builtin.debug", {}).get("msg", ""))
    assert "stays OPEN" in msg, (
        "the refusal does not state the outcome, so it reads as a step that "
        "was skipped rather than a decision that was taken")
    assert "console" in msg, (
        "the refusal does not say what the remaining path would be, which is "
        "the whole reason it refuses")
    assert "lockdown again" in msg, (
        "the refusal does not say how to reach the other shape, so a client "
        "who wants the port closed is told only that it is not")


# --- shape two: tailnet, 22 closed after proof ------------------------------

def test_the_close_is_still_gated_on_a_proof_from_the_controller():
    """The sequence that keeps this from being a lockout, unchanged by the
    method split: add -> PROVE from the machine about to lose the old path ->
    remove. Detection is not proof: an interface can be up while the route
    through it is dead."""
    tasks = _flatten(_load(LOCKDOWN_TASKS))
    add = _index(tasks, "Allow SSH on tailscale0")
    proof = _index(tasks, "Verify tailnet SSH reachability")
    remove = _index(tasks, "Remove the public SSH allow rule")
    assert add < proof < remove, (
        f"the order is add={add} proof={proof} remove={remove}; the proof has "
        "to sit between them or it reports the outcome instead of gating it")
    verify = tasks[proof]
    assert verify.get("delegate_to") == "localhost", (
        "the proof runs on the host, which proves the connection Ansible "
        "already has rather than the one the operator needs next")


def test_the_registry_declaration_narrows_only_after_the_proof():
    """The port is a registry entry and the registry's reconciler runs on a
    timer, so the declaration decides the firewall long after this play ends.
    Narrowing it before the proof would close 22 on the next timer fire,
    whatever this play then decided."""
    tasks = _flatten(_load(LOCKDOWN_TASKS))
    proof = _index(tasks, "Verify tailnet SSH reachability")
    declare = _index(tasks, "declare port 22 private")
    assert proof < declare, (
        "the declaration narrows before the tailnet path is proven")
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
