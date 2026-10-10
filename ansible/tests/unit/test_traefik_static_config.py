"""catena-traefik routes from its file provider alone.

A docker or swarm provider would read routes from any container's or service's
labels, so a client app labelled traefik.enable=true with a router on a Catena
host, at a priority above the gates' 100, would take that host past every check
dashboard-sync makes before it writes an app's route. With the file provider
the only one, a route exists only as a file a Catena producer wrote.

The template goes through ansible-core's own templating, the way the converge
renders it.

Run: uv run pytest tests/unit/test_traefik_static_config.py
"""
from __future__ import annotations

from pathlib import Path

import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template

ROLE = Path(__file__).resolve().parents[2] / "reconcile" / "roles" / "traefik"
DEFAULTS = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
DYNAMIC_DIR = "/etc/catena/traefik/dynamic"


def _static_config() -> dict:
    text = Templar(loader=DataLoader(), variables={
        "ansible_managed": "managed",
        "traefik_dynamic_dir": DYNAMIC_DIR,
        "traefik_trusted_ips": DEFAULTS["traefik_trusted_ips"],
    }).template(trust_as_template((ROLE / "templates" / "traefik.yml.j2").read_text()))
    return yaml.safe_load(text)


def test_the_file_provider_is_the_only_provider():
    assert _static_config()["providers"] == {
        "file": {"directory": DYNAMIC_DIR, "watch": True},
    }, "catena-traefik reads routes from somewhere other than its dynamic directory"
