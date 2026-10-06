"""clamd's database reload is blocking on a host below the memory line and
concurrent above it, decided from the memory the converge gathers.

A concurrent reload loads the new signature database beside the old one, so
for the length of the load clamd holds both: about 1 GB more. On a small host
with no swap that tips the guest into thrashing. Below
``clamav_blocking_reload_below_mb`` the compose renders
``CLAMD_CONF_ConcurrentDatabaseReload: "no"``, which the clamav/clamav
entrypoint writes into clamd.conf at container start; a changed value changes
the service spec, so the deploy replaces the task.

The template is rendered the way the converge renders it, with the role's
defaults and the gathered ``ansible_facts``, and read back as YAML.

Run: uv run pytest tests/unit/test_clamd_reload_follows_host_memory.py
"""
from __future__ import annotations

from pathlib import Path

import jinja2
import pytest
import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
ROLE = ANSIBLE / "reconcile" / "roles" / "infrastructure"
TEMPLATE = ROLE / "templates" / "clamav.compose.yml.j2"
DEFAULTS = ROLE / "defaults" / "main.yml"


def _defaults() -> dict:
    return yaml.safe_load(DEFAULTS.read_text())


def _environment(memtotal_mb: int, **overrides) -> dict:
    """The clamav service's environment for a host reporting `memtotal_mb`."""
    defaults = _defaults()
    context = {
        "ansible_managed": "",
        "ansible_facts": {"memtotal_mb": memtotal_mb},
        "clamav_image": "clamav/clamav:test",
        "clamav_network": defaults["clamav_network"],
        "clamav_stream_max_length": defaults["clamav_stream_max_length"],
        "clamav_blocking_reload_below_mb": defaults["clamav_blocking_reload_below_mb"],
        **overrides,
    }
    rendered = jinja2.Environment(undefined=jinja2.StrictUndefined).from_string(
        TEMPLATE.read_text()).render(**context)
    return yaml.safe_load(rendered)["services"]["clamav"]["environment"]


@pytest.mark.parametrize("memtotal_mb, mode", [
    (3913, "no"),     # a 4 GiB host
    (7876, "yes"),    # an 8 GiB host, as the kernel reports it
    (16008, "yes"),   # a 16 GiB host
])
def test_the_reload_mode_follows_the_hosts_memory(memtotal_mb, mode):
    env = _environment(memtotal_mb)
    assert env["CLAMD_CONF_ConcurrentDatabaseReload"] == mode


def test_the_line_is_the_role_default():
    """The template compares with the knob, not with a number of its own: a
    host just under the default reloads blocking, one at it concurrently, and
    a raised knob takes a 16 GiB host below the line."""
    line = _defaults()["clamav_blocking_reload_below_mb"]
    assert isinstance(line, int) and line > 0
    assert _environment(line - 1)["CLAMD_CONF_ConcurrentDatabaseReload"] == "no"
    assert _environment(line)["CLAMD_CONF_ConcurrentDatabaseReload"] == "yes"
    raised = _environment(16008, clamav_blocking_reload_below_mb=32768)
    assert raised["CLAMD_CONF_ConcurrentDatabaseReload"] == "no"


def test_the_stream_limit_reaches_clamd():
    env = _environment(16008)
    assert env["CLAMD_CONF_StreamMaxLength"] == _defaults()["clamav_stream_max_length"]


def test_both_converge_paths_gather_the_memory_fact():
    """The option reads ansible_facts.memtotal_mb, which only fact gathering
    sets: on the host's own converge (reconcile.yml) and on the full one
    (converge.yml) alike. A play that stopped gathering, or narrowed the
    subset, would fail the render on every converge."""
    for name in ("reconcile.yml", "converge.yml"):
        plays = yaml.safe_load((ANSIBLE / "playbooks" / name).read_text())
        play = next(p for p in plays
                    if any((r.get("role") if isinstance(r, dict) else r) == "infrastructure"
                           for r in p.get("roles", [])))
        assert play.get("gather_facts") is True, name
        assert "gather_subset" not in play, name
