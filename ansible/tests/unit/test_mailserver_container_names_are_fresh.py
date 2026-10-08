"""A swarm task name is true for an instant, so it is resolved where it is used.

docker-mailserver restarts its way out of the very state the mailserver chain
repairs, and every restart mints a new swarm task id, so a name captured a few
tasks earlier is routinely dead by the time it is used. Any `docker exec` /
`docker cp` against a mailserver container resolves the name INSIDE its own
command, and a script that runs several commands against one container makes
the lookup and the commands one retried unit.

`| length` gates on an already-resolved name are fine: they ask whether the
stack is deployed, which does not go stale the same way -- a stack present at
the start of the play is present at the end of it.

Run: uv run pytest tests/unit/test_mailserver_container_names_are_fresh.py
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
TASKS = ANSIBLE / "reconcile" / "roles" / "infrastructure" / "tasks"

# Names the chain resolves once and must not interpolate into a docker command.
CAPTURED = ("mailserver_dms_ct", "_roundcube_ct.stdout", "_ms_cert_dms",
            "_ms_acct_dms", "_dms_ct.stdout")

MAILSERVER_FILES = sorted(TASKS.glob("mailserver_*.yml")) + [
    TASKS / "_mailserver_dms_locate.yml"
]

CERT_HOOK = ANSIBLE / "scripts" / "mailserver-cert-reload.sh"


def _tasks(path: Path) -> list[dict]:
    return [t for t in (yaml.safe_load(path.read_text()) or [])
            if isinstance(t, dict)]


def _command_text(task: dict) -> str:
    """Everything this task actually executes, across the module shapes the
    chain uses (command/argv, command/cmd, shell)."""
    out: list[str] = []
    for key in ("ansible.builtin.command", "ansible.builtin.shell"):
        mod = task.get(key)
        if isinstance(mod, str):
            out.append(mod)
        elif isinstance(mod, dict):
            argv = mod.get("argv")
            if isinstance(argv, list):
                out.append(" ".join(str(a) for a in argv))
            for f in ("cmd", "_raw_params"):
                if mod.get(f):
                    out.append(str(mod[f]))
    return "\n".join(out)


def test_there_are_mailserver_files_to_check() -> None:
    """Guard the guard: a rename would make this vacuous."""
    assert len(MAILSERVER_FILES) >= 4, MAILSERVER_FILES


def test_no_docker_command_uses_a_previously_resolved_container_name() -> None:
    offenders: list[str] = []
    for path in MAILSERVER_FILES:
        if not path.is_file():
            continue
        for task in _tasks(path):
            text = _command_text(task)
            if "docker exec" not in text and "docker cp" not in text:
                if not re.search(r"\bdocker\b.*\b(exec|cp)\b", text):
                    continue
            for var in CAPTURED:
                if "{{ " + var in text or "{{" + var in text:
                    offenders.append(
                        f"{path.name}: {task.get('name')!r} interpolates "
                        f"{var} into a docker command")
    assert not offenders, (
        "resolve the container inside the command instead:\n  "
        + "\n  ".join(offenders))


def test_the_locator_itself_still_resolves_from_docker_ps() -> None:
    """The shared locate step is where a `docker ps` belongs."""
    text = (TASKS / "_mailserver_dms_locate.yml").read_text()
    assert "[/usr/local/bin/catena-container, catena-mailserver, dms]" in text
    assert "docker" in text and "service" in text and "ls" in text, (
        "deployment is decided by the swarm SERVICE, which does not blink "
        "between container restarts"
    )


# Files whose body acts on the dms container. Each must go through the shared
# locate rather than sampling `docker ps` once for itself.
_DMS_CONSUMERS = ("mailserver_cert.yml", "mailserver_accounts.yml",
                  "mailserver_oidc.yml", "mailserver_filtering.yml",
                  "mailserver_dns.yml")


def test_every_dms_consumer_uses_the_shared_locate() -> None:
    """A one-shot `docker ps` cannot tell an absent mailserver from one that is
    between restarts, and dms is between restarts by design for as long as this
    chain has not repaired it."""
    offenders = []
    for name in _DMS_CONSUMERS:
        text = (TASKS / name).read_text()
        if "_mailserver_dms_locate.yml" not in text:
            offenders.append(f"{name} does not include the shared locate")
    assert not offenders, "\n  ".join(offenders)


def test_the_deployed_but_absent_verdict_lives_in_the_locate() -> None:
    """One host state, one verdict, in the shared step: a consumer with its own
    answer can skip silently on a converge that reports success."""
    locate = (TASKS / "_mailserver_dms_locate.yml").read_text()
    assert "ansible.builtin.fail" in locate, (
        "the locate no longer fails on a deployed stack with no container"
    )
    assert "mailserver_deployed" in locate

    for name in _DMS_CONSUMERS:
        text = (TASKS / name).read_text()
        assert "ansible.builtin.fail" not in text, (
            f"{name} carries its own verdict for a state the locate decides."
        )


def test_filtering_does_not_name_the_container_in_a_docker_call() -> None:
    """These are `command: argv:` tasks with loops and sha comparisons, so the
    inline prelude the other files use has nowhere to live.
    /usr/local/bin/catena-dms-exec does the lookup per invocation."""
    text = (TASKS / "mailserver_filtering.yml").read_text()
    assert "catena-dms-exec" in text, (
        "filtering no longer routes its docker calls through the helper"
    )
    body = text.split("block:", 1)[1]
    assert "- docker" not in body, (
        "a docker call in filtering's body names the container directly again"
    )


def _inject_body() -> str:
    """The deploy hook's retried unit: from `inject_once() {` to the closing
    brace in column 0."""
    lines = CERT_HOOK.read_text().splitlines()
    start = next(
        (i for i, ln in enumerate(lines) if ln.startswith("inject_once()")), None)
    assert start is not None, (
        "the hook has no inject_once(); the lookup and the commands using it "
        "are no longer one retried unit"
    )
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start:end])


def test_the_cert_hook_reresolves_inside_the_unit_it_retries() -> None:
    """Every docker command that names the container sits with the lookup that
    produced the name, so a retry gets a NEW name rather than re-running
    against the same dead one."""
    body = _inject_body()
    assert "catena-container catena-mailserver dms" in body, (
        "the retried unit does not look the container up; it is reusing a name "
        "resolved outside the loop"
    )
    inside = {b.strip() for b in body.splitlines()}
    stray = [
        ln.strip() for ln in CERT_HOOK.read_text().splitlines()
        if not ln.lstrip().startswith("#")
        and re.search(r"\bdocker\s+(exec|cp)\b", ln)
        and ln.strip() not in inside
    ]
    assert not stray, (
        "these docker commands run outside the retried unit, so they still "
        "assume the container outlives the lookup:\n  " + "\n  ".join(stray))


def test_the_cert_hook_bounds_its_wait_and_fails_loudly() -> None:
    """dms cycling forever is a real failure, and the hook has to say so --
    exiting 0 on it would hand the converge a host with no mail cert and call
    it done."""
    text = CERT_HOOK.read_text()
    assert "while [ \"$attempt\" -le" in text, "the wait is not a bounded loop"
    assert re.search(r"^exit 1$", text, re.M), (
        "the hook never fails; a dms that never comes up would pass silently"
    )


# The Webmail-link hook is the same rule on the Nextcloud side: a converge that
# updates the nextcloud stack replaces the task underneath it, and `docker
# exec` on a killed container exits 137.
WEBMAIL_HOOK = ANSIBLE / "scripts" / "wire-nextcloud-webmail-link.sh"
LOOKUP = ANSIBLE / "scripts" / "catena-container.sh"


def test_the_webmail_hook_resolves_the_container_inside_every_exec() -> None:
    """No `docker exec` may name a container resolved once at the top."""
    lines = [
        ln.strip() for ln in WEBMAIL_HOOK.read_text().splitlines()
        if not ln.lstrip().startswith("#")
        and re.search(r"\bdocker\s+exec\b", ln)
    ]
    assert lines, "the hook no longer execs into Nextcloud at all"
    stale = [ln for ln in lines if '"$ct"' in ln or "${ct}" in ln]
    assert not stale, (
        "these exec a container name captured before the command ran, so a "
        "swarm task roll kills them mid-script:\n  " + "\n  ".join(stale))


def test_the_webmail_hook_retries_the_config_write_as_one_unit() -> None:
    """The eight config:system:set calls write one entry between them, so a
    roll partway through leaves it half-written. Replaying the whole block is
    what converges it."""
    text = WEBMAIL_HOOK.read_text()
    assert "configure_once()" in text, (
        "the config writes are not grouped into a retryable unit"
    )
    body = text.split("configure_once()", 1)[1].split("\n}", 1)[0]
    assert body.count("config:system:set") == 8, (
        "some config:system:set calls sit outside the retried unit, so a "
        "retry would write a partial entry"
    )
    assert re.search(r"for attempt in .*\n\s*if configure_once", text), (
        "configure_once is never retried"
    )


def test_the_webmail_hook_fails_loudly_when_the_task_never_settles() -> None:
    """A container that keeps vanishing is a host that is not converging. The
    appstore branch above may fail open on one unreachable upstream; this one
    must not, or the converge reports a link it never wrote."""
    text = WEBMAIL_HOOK.read_text()
    assert re.search(r"^\s*exit 1$", text, re.M), (
        "the hook can exhaust its retries and still exit 0"
    )


def test_the_webmail_hook_separates_not_deployed_from_could_not_look() -> None:
    """An empty `docker ps` means Nextcloud is not deployed, which is a
    legitimate skip. A `docker ps` that FAILED is not that answer, and folding
    the two together is how a probe reports success for never having looked."""
    assert "catena-container catena-nextcloud app" in WEBMAIL_HOOK.read_text()
    lookup = LOOKUP.read_text()
    assert "docker ps failed while resolving" in lookup and "exit 2" in lookup, (
        "a failed docker ps is indistinguishable from an absent stack"
    )
