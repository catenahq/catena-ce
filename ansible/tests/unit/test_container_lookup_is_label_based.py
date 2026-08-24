"""No converge task may find a container by a compose label or by its name.

Every application on a catena host is a swarm service. `docker stack deploy`
puts `com.docker.stack.namespace` and `com.docker.swarm.service.name` on a
task and does NOT put `com.docker.compose.project` or
`com.docker.compose.service` on anything, so a filter on either matches
nothing -- silently. `docker ps --filter` with no match is an empty list and
exit 0, which every one of these call sites reads as "that application is not
deployed":

  clamd never deploys, so the mail path is unscanned
  coturn never deploys, so Talk and Jitsi have no relay
  the mailserver tasks all no-op, so DKIM, the TLS cert and the OIDC config
      are never written

None of that fails a converge. It reports success for a host with half its
wiring missing, which is why this is a gate rather than a convention.

Container NAMES are out too: a name carries whatever stack name a client
typed into Portainer's deploy form. The catalog labels every service with
`vps.app` and `vps.component`, and those are what a lookup asks for.

Run: uv run pytest tests/unit/test_container_lookup_is_label_based.py
"""
from __future__ import annotations

import re
from pathlib import Path

ANSIBLE = Path(__file__).resolve().parents[2]

# Vendored collections carry their own examples; never our code.
_SKIP_PARTS = (".collections", "ansible_collections", ".git", "node_modules")

_COMPOSE_LABEL = re.compile(r"label=com\.docker\.compose\.\w+")
# `docker ps --filter name=<x>` -- matching a container by name shape.
_NAME_FILTER = re.compile(r"--filter\s+'?name=")


def _sources() -> list[Path]:
    out = []
    for suffix in (".yml", ".yaml", ".py", ".sh", ".j2"):
        for path in ANSIBLE.rglob(f"*{suffix}"):
            if any(part in _SKIP_PARTS for part in path.parts):
                continue
            if path.parent.name == "unit" and path.name.startswith("test_"):
                continue
            out.append(path)
    return sorted(out)


def test_no_task_filters_on_a_compose_label():
    offenders = []
    for path in _sources():
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in _COMPOSE_LABEL.finditer(text):
            line = text[:match.start()].count("\n") + 1
            offenders.append(f"{path.relative_to(ANSIBLE)}:{line}: {match.group(0)}")
    assert not offenders, (
        "a swarm task carries no com.docker.compose.* label, so these filters "
        "match nothing and the caller reads it as 'not deployed':\n  "
        + "\n  ".join(offenders)
        + "\nUse label=vps.app=<app> / label=vps.component=<service>."
    )


def test_no_task_finds_a_container_by_name_shape():
    """`docker service ls --filter name=` is fine -- a SERVICE name is
    declared by the converge. A CONTAINER name is not: it carries the stack
    name a client typed."""
    offenders = []
    for path in _sources():
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in _NAME_FILTER.finditer(text):
            start = text.rfind("docker", 0, match.start())
            if start != -1 and "service ls" in text[start:match.start()]:
                continue
            line = text[:match.start()].count("\n") + 1
            offenders.append(f"{path.relative_to(ANSIBLE)}:{line}")
    assert not offenders, (
        "these look up a CONTAINER by name, which depends on the stack name a "
        "client typed into Portainer:\n  " + "\n  ".join(offenders)
    )
