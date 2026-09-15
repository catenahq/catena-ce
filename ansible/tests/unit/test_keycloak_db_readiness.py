"""reconcile/roles/keycloak must wait for postgres to ACCEPT CONNECTIONS, not exist.

`fi_a2_oidc_secret_rotation` failed its converge in run
2026-08-02T01-47-33-2e7d:

    psql: error: connection to server on socket
    "/var/run/postgresql/.s.PGSQL.5432" failed:
    FATAL:  the database system is starting up

provision_db.yml retried finding the catena-postgres swarm TASK properly and
then ran three `docker exec ... psql` calls with no readiness gate at all. A
container that exists is not a server that answers, and after a rewind the gap
between the two is seconds wide.

This is a product defect, not a bench one: it fails a client converge. The
test pins the property rather than the wording, so the next `docker exec`
added to this file cannot quietly skip the gate.

Run: uv run pytest tests/unit/test_keycloak_db_readiness.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
TASKS = ANSIBLE / "reconcile" / "roles" / "keycloak" / "tasks" / "provision_db.yml"


def _tasks() -> list[dict]:
    return yaml.safe_load(TASKS.read_text())


def _module_body(task: dict) -> str:
    """Everything this task will actually run, as one string."""
    parts: list[str] = []
    for key, val in task.items():
        if key in ("name", "register", "when", "vars", "args", "changed_when",
                   "failed_when", "until", "retries", "delay", "no_log",
                   "loop", "tags"):
            continue
        if isinstance(val, str):
            parts.append(val)
        elif isinstance(val, dict):
            for v in val.values():
                if isinstance(v, str):
                    parts.append(v)
                elif isinstance(v, list):
                    parts.extend(str(x) for x in v)
        elif isinstance(val, list):
            parts.extend(str(x) for x in val)
    return "\n".join(parts)


def test_a_readiness_gate_exists() -> None:
    tasks = _tasks()
    assert any("pg_isready" in _module_body(t) for t in tasks), (
        "provision_db.yml runs psql against a container it only proved EXISTS. "
        "pg_isready checks server acceptance with no auth, no role and no "
        "database, so it needs none of the facts the tasks after it derive."
    )


def test_the_gate_retries_rather_than_probing_once() -> None:
    """A single probe on a starting server is a coin flip, not a gate."""
    gate = next(t for t in _tasks() if "pg_isready" in _module_body(t))
    assert gate.get("until"), "the readiness gate does not wait for anything"
    assert int(gate.get("retries", 0)) >= 10
    assert int(gate.get("delay", 0)) >= 1
    # A probe that reports changed on every converge would fail the
    # idempotency rerun for asking a question.
    assert gate.get("changed_when") is False


def test_the_gate_precedes_every_psql() -> None:
    """It goes before the POSTGRES_USER probe, not after: that probe is
    itself a `docker exec` and races the same window."""
    tasks = _tasks()
    gate_at = next(
        i for i, t in enumerate(tasks) if "pg_isready" in _module_body(t)
    )
    psql_at = [
        i for i, t in enumerate(tasks)
        if "psql" in _module_body(t) and "pg_isready" not in _module_body(t)
    ]
    assert psql_at, "no psql task found -- has this file been restructured?"
    assert gate_at < min(psql_at), (
        f"the readiness gate is task {gate_at} but a psql runs at "
        f"{min(psql_at)}. Everything that talks to the database has to come "
        "after the gate, or the gate is decoration."
    )


def test_the_gate_precedes_every_docker_exec() -> None:
    """Wider than psql: the POSTGRES_USER probe shells into the same
    container and fails the same way if it lands in the starting window."""
    tasks = _tasks()
    gate_at = next(
        i for i, t in enumerate(tasks) if "pg_isready" in _module_body(t)
    )
    execs = [
        i for i, t in enumerate(tasks)
        if "docker exec" in _module_body(t) or (
            "docker" in _module_body(t) and "exec" in _module_body(t)
            and "pg_isready" not in _module_body(t)
        )
    ]
    later = [i for i in execs if i > gate_at]
    earlier = [i for i in execs if i < gate_at]
    assert later, "no docker exec after the gate -- the gate guards nothing"
    assert not earlier, (
        f"docker exec tasks at {earlier} run BEFORE the readiness gate at "
        f"{gate_at}"
    )


def test_the_gate_names_a_role_that_exists() -> None:
    """`docker exec` runs as root inside the container, so a pg_isready with
    no -U puts `root` in the startup packet and postgres logs
    `FATAL: role "root" does not exist` -- once per attempt, up to the 30
    retries above.

    Not about auth: pg_isready exits 0 whenever the server responds at all,
    error included, which is exactly why this survived unnoticed until the
    bench's stage-3f control-plane log gate read the log on run
    2026-08-06T20-24-17-5ccd. It is about what postgres WRITES, because a
    real auth failure is indistinguishable inside that noise.
    """
    gate = next(t for t in _tasks() if "pg_isready" in _module_body(t))
    # Read the command TEXT, not an argv list: the gate re-resolves its
    # container per attempt now (a swarm task id changes on every reschedule,
    # and postgres restarting is the very thing being waited on), so it is a
    # shell rather than a command/argv. This assertion is about what reaches
    # pg_isready, which is the same either way.
    body = _module_body(gate)
    tokens = body.split()
    assert "-U" in tokens, (
        "pg_isready runs without -U, so it authenticates as root and postgres "
        "logs a FATAL on every attempt. Both sibling call sites in "
        "reconcile/roles/postgres already pass -U.\n" + body
    )
    user = tokens[tokens.index("-U") + 1]
    assert user.strip(), "-U was passed with no role after it"
    assert "root" not in user, (
        f"-U resolves to {user!r}; root is the very role that does not exist"
    )
