"""Compose keys `docker stack deploy` rejects, or accepts and ignores.

Every template here is deployed by reconcile/roles/infrastructure/tasks/swarm_stack.yml.
Swarm reads a SUBSET of the compose spec, and the two ways it disagrees with
`docker compose` have opposite failure modes:

  REJECTED, loudly -- `host_ip` in a port's long syntax. Swarm's PortConfig
      carries no host IP, and the CLI refuses the key outright:
      "services.app.ports.0 Additional property host_ip is not allowed".
      That failure is cheap: the converge stops.

  IGNORED, silently -- `restart:`, `network_mode:`. The stack deploys, the
      warning scrolls past, and the service runs with swarm's defaults
      instead of what the file asked for. beszel-agent is the sharp edge:
      with `network_mode: host` ignored it lands on an overlay, reports that
      namespace's metrics as the host's, and dials a 127.0.0.1 hub that is
      not there -- a monitoring agent that looks healthy and measures the
      wrong machine.

The loopback publishes are 0.0.0.0 binds, held loopback-only by a
`scope: loopback` declaration in the public-port registry -- see
test_public_ports_loopback_scope.py.

The app-level vps.* labels (route, access, health, names) are read off client
apps alone, so on these stacks they deploy and nothing reads them.

Run: uv run pytest tests/unit/test_swarm_compose_shape.py
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ANSIBLE = Path(__file__).resolve().parents[2]

# Every compose template deployed through swarm_stack.yml.
SWARM_COMPOSE = (
    ANSIBLE / "reconcile/roles/infrastructure/templates/beszel-agent.compose.yml.j2",
    ANSIBLE / "reconcile/roles/infrastructure/templates/beszel-hc-shim.compose.yml.j2",
    ANSIBLE / "reconcile/roles/keycloak/templates/keycloak.compose.yml.j2",
    ANSIBLE / "reconcile/roles/oauth2_proxy/templates/oauth2-proxy.compose.yml.j2",
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
    the DELAY: with none, a service whose dependency is not up yet hot-loops."""
    body = _body(path)
    assert body.count("restart_policy:") >= 1, (
        f"{path.name}: no deploy.restart_policy"
    )
    assert "delay:" in body, f"{path.name}: restart_policy with no delay"


_APP_LABEL = re.compile(r"vps\.(?:auth|route|health|homepage)\.[\w.-]*|vps\.display-name")


@pytest.mark.parametrize("path", SWARM_COMPOSE, ids=lambda p: p.name)
def test_no_app_level_vps_label(path: Path):
    """The app intent reads these labels off client-app stacks alone (catena-admin
    payload/lib/docker_api.py INFRA_STACKS lists these), and a gated tool's
    gate is its reconcile/roles/oauth2_proxy oauth2_proxy_apps entry."""
    found = _APP_LABEL.findall(_body(path))
    assert not found, (
        f"{path.name}: {found} -- nothing reads an app-level vps.* label on a "
        f"Catena stack. A gate belongs in oauth2_proxy_apps."
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
