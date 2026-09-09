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


def _logical_lines(text: str) -> list[tuple[int, str]]:
    """`(line_number, command)` with shell continuations joined.

    A pipeline written across lines is one command, and the two halves of the
    defect below land on different lines: `docker ps` on the first, the `grep`
    that filters it on the second.
    """
    joined: list[tuple[int, str]] = []
    first: int | None = None
    parts: list[str] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if first is None:
            first = number
        if stripped.endswith("\\"):
            parts.append(stripped[:-1].rstrip())
            continue
        parts.append(stripped)
        joined.append((first, " ".join(parts)))
        parts, first = [], None
    if parts:
        joined.append((first or 0, " ".join(parts)))

    # A pipeline may also continue onto a line that simply STARTS with `|`,
    # with no backslash on the line before it.
    out: list[tuple[int, str]] = []
    for number, command in joined:
        if out and command.startswith("|"):
            prev_number, prev = out[-1]
            out[-1] = (prev_number, f"{prev} {command}")
        else:
            out.append((number, command))
    return out


# A regex quantifier over an alphanumeric class -- `[0-9a-z]{6,}` and friends.
# This is what a pattern looks like when it is trying to match the RANDOM part
# of a name rather than a name.
_RANDOM_SUFFIX_PATTERN = re.compile(r"\[[0-9a-zA-Z-]+\]\{\d+,?\d*\}")


def test_no_task_greps_for_the_random_half_of_a_stack_name():
    """A `docker ps` grep that matches the generated part of a name is the same
    defect as a `--filter name=`, wearing different clothes.

    reconcile/roles/infrastructure/tasks/wordpress_plugins.yml matched
    `wordpress-[0-9a-z]{6,}-wp-`: the stack name Portainer generated, plus a
    swarm task id. A client who deploys the template under a name of their own
    choosing produced no match, the whole block skipped, and the converge
    reported success on a site with no plugin curation and no option seeding.
    Neither test above saw it -- there is no compose label and no
    `--filter name=`, just a regex on the same untrustworthy string.

    DELIBERATELY NARROW. Plenty of tasks grep `docker ps` for a FIXED name, and
    those are fine when the name is one the converge declares: catena-keycloak
    is called that because this tree named it. What cannot be trusted is the
    half a client's deploy form decides, and a quantified character class is
    the tell that a pattern is reaching for it.
    """
    offenders = []
    for path in _sources():
        text = path.read_text(encoding="utf-8", errors="replace")
        for number, command in _logical_lines(text):
            if "docker ps" not in command or "grep" not in command:
                continue
            if "--filter label=" in command or "-f label=" in command:
                continue
            if not _RANDOM_SUFFIX_PATTERN.search(command):
                continue
            offenders.append(f"{path.relative_to(ANSIBLE)}:{number}")
    assert not offenders, (
        "these match the generated half of a container name, which is the "
        "stack name a client typed plus a swarm task id:\n  "
        + "\n  ".join(offenders)
        + "\nUse docker ps --filter label=vps.app=<app> "
          "--filter label=vps.component=<service>."
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
