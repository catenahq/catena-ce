"""Compose keys `docker stack deploy` rejects, or accepts and ignores.

Every template here is deployed by reconcile/roles/infrastructure/tasks/swarm_stack.yml.
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
    ANSIBLE / "reconcile/roles/infrastructure/templates/gatus.compose.yml.j2",
    ANSIBLE / "reconcile/roles/infrastructure/templates/healthchecks.compose.yml.j2",
    ANSIBLE / "reconcile/roles/infrastructure/templates/clamav.compose.yml.j2",
    ANSIBLE / "reconcile/roles/infrastructure/templates/beszel-hub.compose.yml.j2",
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
    the DELAY: with none, a service whose dependency is not up yet hot-loops.
    clamd re-pulls a ~250 MB signature DB on each of those."""
    body = _body(path)
    assert body.count("restart_policy:") >= 1, (
        f"{path.name}: no deploy.restart_policy"
    )
    assert "delay:" in body, f"{path.name}: restart_policy with no delay"


def test_clamav_is_a_swarm_stack_on_an_overlay():
    """The two halves have to stay consistent, and which way they point
    changed with the catalog.

    A swarm service cannot attach to a bridge, and `docker stack deploy`
    refuses a stack whose external network is local-scope at all -- so a
    bridge here would stop the mailserver and Nextcloud templates from
    deploying, not merely keep clamd off swarm.

    The deadlock a bridge avoids: an attachable overlay is materialized on a
    node only once a swarm task there uses it, and a STANDALONE container asking
    for an unmaterialized one stays Created ("network catena-clamav not found",
    bench 050b). With standalone consumers the only swarm task that could
    materialize the network is clamd, which does not deploy until a consumer
    runs. Both consumers are swarm services, so the first of them materializes
    it and the deadlock has no hold.
    """
    tasks = (ANSIBLE / "reconcile/roles/infrastructure/tasks/clamav.yml").read_text(
        encoding="utf-8")
    assert "include_tasks: swarm_stack.yml" in tasks
    assert "include_tasks: portainer_stack.yml" not in tasks
    assert "overlay" in tasks and "--attachable" in tasks

    compose = (
        ANSIBLE / "reconcile/roles/infrastructure/templates/clamav.compose.yml.j2"
    ).read_text(encoding="utf-8")
    assert "restart: unless-stopped" not in compose, (
        "`restart:` is dropped by docker stack deploy with a warning nobody "
        "reads"
    )


def test_the_clamav_consumers_are_matched_by_label():
    """clamd deploys only when a consumer is running, and the probe that
    decides reads vps.app / vps.component. A container NAME depends on
    docker's scheme and on whatever a client typed into Portainer, so a probe
    that matched one would quietly stop finding either consumer and clamd
    would never deploy again -- with the mail path silently unscanned."""
    tasks = (ANSIBLE / "reconcile/roles/infrastructure/tasks/clamav.yml").read_text(
        encoding="utf-8")
    assert "label=vps.app=" in tasks
    assert "label=vps.component=dms" in tasks
    assert "com.docker.compose" not in tasks
    assert "--filter 'name=" not in tasks


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
