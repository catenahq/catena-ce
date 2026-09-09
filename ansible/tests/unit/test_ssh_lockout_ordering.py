"""The sshd handoff proves the new path before it closes the old ones.

THE DEFECT. bootstrap/roles/common performs the second of the two irreversible access
handoffs in the product, and only the first one had a gate:

    ufw_lockdown.yml:  add tailnet rule -> VERIFY -> remove public 22
    bootstrap/roles/common:      install ops key  ->   ?    -> deny everything else

What sat in that gap was `ops_ssh_public_keys | length > 0` -- an assert on a
controller-side STRING. It proves a variable is not empty. It does not prove
sshd accepts that key for ops. The next task then disabled password auth and
root login and restricted AllowUsers to ops in one write.

The correct order, which these tests pin:

    provider account + password
      -> ops + ssh key TESTED
      -> deny the provider account, lock sshd

Ordering is the property, so the tests assert on ORDER, not on presence: a
proof task that exists but runs after the hardening proves nothing, and would
pass every substring check.

Run: uv run pytest tests/unit/test_ssh_lockout_ordering.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
COMMON_TASKS = ANSIBLE / "bootstrap" / "roles" / "common" / "tasks" / "main.yml"
BOOTSTRAP = ANSIBLE / "playbooks" / "bootstrap.yml"


def _tasks(path: Path) -> list[dict]:
    return [t for t in (yaml.safe_load(path.read_text()) or []) if isinstance(t, dict)]


def _index(tasks: list[dict], needle: str) -> int:
    for i, task in enumerate(tasks):
        if needle.lower() in str(task.get("name", "")).lower():
            return i
    raise AssertionError(
        f"no task matching {needle!r}; the ordering it anchors cannot be checked")


# ─── bootstrap/roles/common: the handoff ──────────────────────────────────────────────

def test_ops_key_is_proved_between_installing_it_and_locking_sshd():
    """The whole defect in one assertion. A proof placed after the hardening
    runs on a host that is already locked down -- it would report the outcome
    rather than gate it."""
    tasks = _tasks(COMMON_TASKS)
    install = _index(tasks, "Install SSH authorized keys for ops")
    proof = _index(tasks, "prove ops key auth works")
    harden = _index(tasks, "Drop sshd hardening config")
    assert install < proof, (
        "the key is proved before it is installed, which can only ever fail")
    assert proof < harden, (
        "sshd hardening runs BEFORE ops key auth is proved. That is the "
        "original defect: password auth, root login and every other account "
        "are removed on the strength of a non-empty variable")


def test_the_proof_actually_authenticates_as_ops():
    """`ops_ssh_public_keys | length > 0` passed while the host was
    unreachable. A TCP check would pass too -- sshd completes the handshake
    and THEN refuses the key, which is exactly what the locked-out host
    printed: `Permission denied (publickey)` on an open port.

    So the proof has to be an authentication, as ops, with the operator key.
    """
    tasks = _tasks(COMMON_TASKS)
    proof = _tasks(COMMON_TASKS)[_index(tasks, "prove ops key auth works")]
    argv = proof.get("ansible.builtin.command", {}).get("argv") or []
    argv = [str(a) for a in argv]
    assert argv and argv[0] == "ssh", f"the proof is not an ssh login: {argv}"
    assert any("{{ ops_user }}@" in a for a in argv), (
        f"the proof does not authenticate as ops: {argv}")
    assert "{{ ansible_ssh_private_key_file }}" in argv, (
        "the proof does not pin the operator key, so an agent identity could "
        "satisfy it while the key that ends up on ops does not")
    assert "PreferredAuthentications=publickey" in argv, (
        "without this the proof can pass via password auth -- which the very "
        "next task turns off")
    assert proof.get("delegate_to") == "localhost", (
        "run from the controller; a check that runs ON the host proves the "
        "connection Ansible already has, not the one the operator needs next")
    assert proof.get("failed_when") is False, (
        "the command must not fail the play by itself -- the gate below turns "
        "its rc into a message that says what to do")


def test_a_failed_proof_stops_the_converge():
    """A proof whose result nothing reads is decoration."""
    tasks = _tasks(COMMON_TASKS)
    gate = tasks[_index(tasks, "refuse to harden")]
    assert "ansible.builtin.fail" in gate, "the gate does not stop the play"
    conditions = " ".join(str(c) for c in gate.get("when", []))
    assert "_ops_key_proof.rc" in conditions, (
        "the gate does not read the proof's exit code")
    harden = _index(tasks, "Drop sshd hardening config")
    assert _index(tasks, "refuse to harden") < harden


def test_an_unreadable_key_is_reported_not_silently_passed():
    """The controller not holding the key is a different thing from the key
    not working. It happens for real (a containerised dispatch runner), so it
    must not fail the converge -- and it must not read as a pass either."""
    tasks = _tasks(COMMON_TASKS)
    notice = tasks[_index(tasks, "WITHOUT proof")]
    msg = str(notice.get("ansible.builtin.debug", {}).get("msg", ""))
    assert "unchecked, not verified" in msg, (
        "a skip that does not name itself as a skip reads exactly like a "
        "passing verification")


def test_the_bypass_is_explicit_and_defaults_off():
    """An escape hatch is fine; one that is on by default is the defect with
    extra steps."""
    raw = COMMON_TASKS.read_text()
    assert "skip_ssh_lockout_gate | default(false)" in raw, (
        "the gate's bypass does not default to false")


# ─── bootstrap.yml: the re-run probe ───────────────────────────────────────

def _phase1_pre_tasks() -> list[dict]:
    for play in yaml.safe_load(BOOTSTRAP.read_text()):
        if "Phase 1" in str(play.get("name", "")):
            return [t for t in play.get("pre_tasks", []) if isinstance(t, dict)]
    raise AssertionError("bootstrap.yml has no Phase 1 play")


def test_the_non_root_rerun_probe_targets_ops_not_the_provider_account():
    """Phase 1's own hardening removes the provider account from AllowUsers.
    A probe that runs THROUGH that account therefore fails permanently once
    Phase 1 has run -- reporting "not done" precisely when it is done. The
    play then continued and died connecting as the user it had locked out:

        debian@<ip>: Permission denied (publickey).
        Origin: playbooks/bootstrap.yml  Gather facts now that we know ...

    Net effect: bootstrap was not re-runnable at all on a non-root provider
    image, only on root-login providers. The root path above it already reads
    a failed probe as "already ran"; the non-root path has to probe the
    account Phase 1 CREATES and read success the same way.
    """
    tasks = _phase1_pre_tasks()
    probe = next(t for t in tasks
                 if "idempotency (non-root path)" in str(t.get("name", "")))
    argv = [str(a) for a in probe["ansible.builtin.command"]["argv"]]
    target = next(a for a in argv if "@" in a)
    assert target.startswith("{{ ops_user }}@"), (
        f"the non-root re-run probe still goes through {target!r}. Phase 1 "
        "locks that account out of SSH, so the probe can only ever report "
        "'not done' once it IS done")
    assert "{{ bootstrap_initial_user }}@" not in target


def _phase05_tasks() -> list[dict]:
    for play in yaml.safe_load(BOOTSTRAP.read_text()):
        if "Phase 0.5" in str(play.get("name", "")):
            return [t for t in play.get("tasks", []) if isinstance(t, dict)]
    raise AssertionError("bootstrap.yml has no Phase 0.5 play")


def test_the_key_install_decides_on_the_key_not_on_the_password():
    """Phase 0.5 used to skip itself whenever bootstrap_root_password was
    blank, reasoning "blank means the key is already installed". Nothing
    checked that -- and `catena install` ALWAYS emits the variable, blank when
    install.yaml carries no host_initial_password, deliberately, so a
    vars_prompt cannot stop the deploy chain. Extra-vars outrank vars_prompt,
    so on a fresh VPS driven by the CLI the prompt never appears, the play
    skips, and Phase 1 dies with:

        debian@<ip>: Permission denied (publickey).

    A blank password and an installed key are different states. Probe for the
    state.
    """
    tasks = _phase05_tasks()
    probe = next((t for t in tasks
                  if "key auth already work" in str(t.get("name", ""))), None)
    assert probe is not None, (
        "Phase 0.5 does not probe for the key; it can only infer the host's "
        "state from an input that does not describe it")
    loop = [str(x) for x in (probe.get("loop") or [])]
    assert "{{ bootstrap_initial_user }}" in loop and "{{ ops_user }}" in loop, (
        f"the probe checks {loop}; either account answering means there is "
        "nothing to install -- the provider account on a fresh box, ops on one "
        "Phase 1 already hardened")

    install = next(t for t in tasks if "Install your public key" in str(t.get("name", "")))
    conditions = " ".join(str(c) for c in install.get("when", []))
    assert "_key_works" in conditions, (
        "the install still runs on the password alone, so a host that already "
        "has the key is re-installed and one that needs it is skipped")


def test_a_fresh_host_with_no_password_fails_in_phase_05_not_phase_1():
    """The fix is one input away at this point. Three tasks later it surfaces
    as an unattributed connection error against a task about gathering facts,
    which is exactly how this went undiagnosed."""
    tasks = _phase05_tasks()
    gate = next((t for t in tasks
                 if "no provider password" in str(t.get("name", ""))), None)
    assert gate is not None, "Phase 0.5 has no no-key-and-no-password gate"
    conditions = " ".join(str(c) for c in gate.get("when", []))
    assert "_key_works" in conditions and "bootstrap_root_password" in conditions, (
        "the gate must require BOTH -- no working key AND no password. On "
        "either alone it would fire on a host that is perfectly fine")
    msg = str(gate["ansible.builtin.fail"]["msg"])
    assert "host_initial_password" in msg, (
        "the message does not name the input that fixes it")


def test_neither_account_answering_is_its_own_reported_state():
    """Ops-unreachable and provider-unreachable together is a third state, and
    the one the operator actually hit. Without it the play fails on whichever
    task happens to run next, which is how the defect above went unread for
    so long."""
    tasks = _phase1_pre_tasks()
    names = [str(t.get("name", "")) for t in tasks]
    gate = next((t for t in tasks
                 if "no account answers" in str(t.get("name", ""))), None)
    assert gate is not None, f"no both-probes-failed gate among {names}"
    conditions = " ".join(str(c) for c in gate.get("when", []))
    assert "ops_probe.rc" in conditions and "initial_probe.rc" in conditions, (
        "the gate does not require BOTH probes to have failed; it would fire "
        "on a fresh host, where ops legitimately does not exist yet")
    msg = str(gate["ansible.builtin.fail"]["msg"])
    assert "tailnet" in msg.lower(), (
        "the message does not mention the tailnet address, which is where a "
        "hardened host is actually reachable after site.yml closes public 22")
