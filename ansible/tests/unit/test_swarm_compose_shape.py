"""Compose keys `docker stack deploy` rejects, or accepts and ignores.

Every template here is deployed by roles/infrastructure/tasks/swarm_stack.yml.
Swarm reads a SUBSET of the compose spec, and the two ways it disagrees with
`docker compose` have opposite failure modes:

  REJECTED, loudly -- `host_ip` in a port's long syntax. Swarm's PortConfig
      carries no host IP, and the CLI refuses the key outright:
      "services.app.ports.0 Additional property host_ip is not allowed".
      Caught on bench 050b, which is cheap: the converge stops.

  IGNORED, silently -- `restart:`, `network_mode:`. The stack deploys, the
      warning scrolls past, and the service runs with swarm's defaults
      instead of what the file asked for. beszel-agent is the sharp edge:
      with `network_mode: host` ignored it lands on an overlay, reports that
      namespace's metrics as the host's, and dials a 127.0.0.1 hub that is
      not there -- a monitoring agent that looks healthy and measures the
      wrong machine.

The loopback publishes are the reason `host_ip` was reached for at all. They
stay 0.0.0.0 binds and are held loopback-only by a `scope: loopback`
declaration in the public-port registry instead -- see
test_public_ports_loopback_scope.py.

Run: uv run pytest tests/unit/test_swarm_compose_shape.py
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ANSIBLE = Path(__file__).resolve().parents[2]

# Every compose template deployed through swarm_stack.yml.
SWARM_COMPOSE = (
    ANSIBLE / "roles/infrastructure/templates/gatus.compose.yml.j2",
    ANSIBLE / "roles/infrastructure/templates/healthchecks.compose.yml.j2",
    # clamav.compose.yml.j2 is deliberately ABSENT: clamd is the one
    # catena-declared stack still deployed through the Portainer compose
    # API, so it needs the opposite shape (`restart:`, which swarm ignores
    # and standalone compose reads). tasks/clamav.yml documents why it
    # could not move. test_clamav_stays_on_compose below pins that.
    ANSIBLE / "roles/infrastructure/templates/beszel-hub.compose.yml.j2",
    ANSIBLE / "roles/infrastructure/templates/beszel-agent.compose.yml.j2",
    ANSIBLE / "roles/infrastructure/templates/beszel-hc-shim.compose.yml.j2",
    ANSIBLE / "roles/keycloak/templates/keycloak.compose.yml.j2",
    ANSIBLE / "roles/oauth2_proxy/templates/oauth2-proxy.compose.yml.j2",
)

# Templates are Jinja, so they are scanned as text rather than parsed: a
# `{% if %}` around a service block makes them invalid YAML until rendered.
# Comments are stripped first, because every one of these keys is NAMED in
# the comment explaining why it is absent.
_COMMENT = re.compile(r"^\s*#.*$", re.MULTILINE)


def _body(path: Path) -> str:
    return _COMMENT.sub("", path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", SWARM_COMPOSE, ids=lambda p: p.name)
def test_no_host_ip(path: Path):
    assert "host_ip" not in _body(path), (
        f"{path.name}: docker stack deploy REFUSES host_ip -- the whole "
        f"deploy fails. Declare the port scope=loopback in the public-port "
        f"registry instead."
    )


@pytest.mark.parametrize("path", SWARM_COMPOSE, ids=lambda p: p.name)
def test_no_ignored_compose_only_keys(path: Path):
    body = _body(path)
    for key in ("restart:", "network_mode:"):
        assert key not in body, (
            f"{path.name}: `{key}` is a compose-only key that swarm IGNORES. "
            f"The stack deploys and the service silently runs with swarm's "
            f"defaults. Use deploy.restart_policy / an external `host` "
            f"network."
        )


@pytest.mark.parametrize("path", SWARM_COMPOSE, ids=lambda p: p.name)
def test_every_service_declares_a_restart_policy(path: Path):
    """Swarm's default restart condition is already `any`, so this is about
    the DELAY: with none, a service whose dependency is not up yet hot-loops.
    clamd re-pulls a ~250 MB signature DB on each of those."""
    body = _body(path)
    assert body.count("restart_policy:") >= 1, (
        f"{path.name}: no deploy.restart_policy"
    )
    assert "delay:" in body, f"{path.name}: restart_policy with no delay"


def test_clamav_stays_on_compose_with_a_local_bridge():
    """clamd is the exception, and the two halves have to stay consistent.

    A swarm service cannot attach to a bridge, and an attachable OVERLAY is
    only materialized on a node once a swarm task there uses it -- so with
    clamd behind its consumer gate, the standalone consumers that need the
    network could never start, and the only thing that would materialize it
    for them is clamd. Bench 050b: nextcloud-app-1 Created, "network
    catena-clamav not found".

    So the network stays a local bridge and the stack stays on compose. If
    one half is ever flipped without the other, clamd silently stops
    deploying or its consumers silently stop starting."""
    tasks = (ANSIBLE / "roles/infrastructure/tasks/clamav.yml").read_text(
        encoding="utf-8")
    # Match the include DIRECTIVE, not any mention: the file's header
    # explains the swarm_stack.yml it deliberately does not use.
    assert "include_tasks: portainer_stack.yml" in tasks
    assert "include_tasks: swarm_stack.yml" not in tasks
    assert "--driver, overlay" not in tasks

    compose = (
        ANSIBLE / "roles/infrastructure/templates/clamav.compose.yml.j2"
    ).read_text(encoding="utf-8")
    assert "restart: unless-stopped" in compose, (
        "standalone compose reads `restart:` and ignores deploy.restart_policy"
    )


def test_published_ports_use_long_syntax_host_mode():
    """The short `<published>:<target>` form deploys as an INGRESS publish
    under swarm, which puts the port on the routing mesh -- reachable from
    any node, and NOT what a host-side reader on this box needs."""
    for path in SWARM_COMPOSE:
        body = _body(path)
        if "ports:" not in body:
            continue
        assert "mode: host" in body, f"{path.name}: publish is not mode: host"
        # The short form is a quoted "a:b" list item under ports:.
        assert not re.search(r'^\s*-\s*"[\d.]+:\d+', body, re.MULTILINE), (
            f"{path.name}: short-form port mapping survives; under swarm it "
            f"becomes an ingress publish"
        )
