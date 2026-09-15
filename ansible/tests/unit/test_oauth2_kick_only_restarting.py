"""The oauth2-proxy kick must not resurrect swarm's replaced tasks.

The task is named "kick oauth2-proxy containers stuck in restart-backoff",
and it selected on `not Up`. `docker ps -a` also lists the task containers
swarm has ALREADY REPLACED -- Exited, Created -- so `docker restart` brought
those back to life. Swarm does not track them, so nothing takes them down
again, and two containers then sit on the overlay under one service alias.
Requests round-robin, and the resurrected one runs the configuration it died
with.

Measured on bench run 2026-09-01T15-47-41-5ea0, on a materialised host:

    docker service ls  -> 4 oauth2-proxy services, each 1/1
    docker ps          -> 8 Up oauth2-proxy containers
    docker service ps oauth2-proxy_oauth2-proxy-gatus
                       -> one task: ...uptm6r604rjb
    docker ps          -> ...uptm6r604rjb AND ...btfqe0dzn41p, both Up

It also made the converge permanently non-idempotent on such a host: a
corpse restarted is a corpse again by the next pass, and changed_when is
unconditional. ce_converge's settle guard is what surfaced it, by refusing
to accept a first-converge change it had not been told to expect.

Run: uv run pytest tests/unit/test_oauth2_kick_only_restarting.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

_VALIDATE = (Path(__file__).resolve().parents[2]
             / "reconcile" / "roles" / "oauth2_proxy" / "tasks" / "validate.yml")

_KICK = "validate/oauth2-proxy: kick oauth2-proxy containers stuck in restart-backoff"


def _kick_task() -> dict:
    def walk(tasks):
        for t in tasks or []:
            if t.get("name") == _KICK:
                return t
            for key in ("block", "rescue", "always"):
                found = walk(t.get(key))
                if found:
                    return found
        return None

    found = walk(yaml.safe_load(_VALIDATE.read_text(encoding="utf-8")))
    assert found, f"{_KICK!r} is gone from {_VALIDATE}"
    return found


def _statuses() -> list[str]:
    return [str(c) for c in _kick_task()["when"]]


def test_only_restarting_containers_are_kicked():
    assert any("startswith('Restarting')" in c for c in _statuses()), _statuses()


def test_the_not_up_selector_is_gone():
    """`not Up` is what swept in Exited and Created -- swarm's business, not
    this task's."""
    assert not any("not (" in c and "startswith('Up')" in c
                   for c in _statuses()), _statuses()


def test_it_still_restarts_rather_than_removing():
    """A container genuinely in OIDC-discovery backoff is skipped past by a
    restart; removing one would be swarm's job and a much bigger hammer."""
    argv = _kick_task()["ansible.builtin.command"]["argv"]
    assert argv[0] == "docker" and argv[1] == "restart"


def test_an_exited_corpse_would_not_match():
    """The states docker actually prints, checked against the guard rather
    than against a paraphrase of it."""
    guard = next(c for c in _statuses() if "startswith(" in c)
    for dead in ("Exited (1) 2 hours ago", "Created", "Up 3 minutes (healthy)"):
        assert not dead.startswith("Restarting"), (
            f"{dead!r} must not satisfy {guard!r}")
    assert "Restarting (1) 5 seconds ago".startswith("Restarting")
