"""Lock the catena-network-nudge boot-time self-heal.

`catena-network` is a swarm-scoped overlay. On a docker.service start
dockerd restores restart-policy containers during "Loading containers",
which runs BEFORE the swarm node re-materializes the overlay -- and the
boot-time restore gets exactly ONE attempt per container, so whatever loses
that race stays Exited with RestartCount=0 forever. Swarm services
re-dispatch themselves; plain/compose containers (keycloak-server-1,
nextcloud-talk-hpb-1, ...) have nothing to heal them, so a plain VPS reboot
could strand Keycloak until the next converge (observed live: bench run
2026-07-15T05-21-38-7c45, overlay up 2.5s after "Loading containers: done").

The nudge widened from traefik-only to every stranded container, then lost
its traefik special case entirely at the swarm conversion: a swarm service
re-dispatches itself by name, and the script already skips swarm task
containers, so traefik heals itself twice over. What it protects now is the
Portainer COMPOSE containers, which is why it lives in roles/docker -- the
race is between dockerd's container restore and the swarm init, both owned by
that role.

What must hold:
  - the heal is gated on the EXACT overlay-not-found error, so a container
    the operator stopped on purpose is never started behind their back
    (the blanket-reap mistake, twice reverted);
  - swarm task containers are skipped -- swarm owns their lifecycle;
  - NO service gets an unconditional start-if-not-running any more, because
    that rule existed only for the plain traefik container;
  - the old traefik-named artifacts are removed, else the stale drop-in
    keeps firing the old script alongside the new one.

Run: uv run pytest tests/unit/test_network_nudge_heals_stranded.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3] / "ansible"
SCRIPT = ROOT / "scripts" / "catena-network-nudge.sh"
TASKS = ROOT / "roles" / "docker" / "tasks" / "main.yml"
UNIT = ROOT / "roles" / "docker" / "templates" / "catena-network-nudge.service.j2"
DROPIN = ROOT / "roles" / "docker" / "templates" / "docker-service-nudge-dropin.conf.j2"


def _find(name_fragment: str) -> dict:
    for t in yaml.safe_load(TASKS.read_text()):
        if name_fragment in (t.get("name") or ""):
            return t
    raise AssertionError(f"task not found: {name_fragment!r}")


def test_heal_is_gated_on_the_overlay_error():
    body = SCRIPT.read_text()
    # The evidence gate: only what docker recorded as overlay-not-found.
    assert '*"network $NET not found"*' in body
    # A deliberately stopped container has no such error -> untouched.
    assert ".State.Error" in body


def test_only_containers_docker_meant_to_keep_running():
    body = SCRIPT.read_text()
    assert "RestartPolicy.Name" in body
    assert "always | unless-stopped" in body


def test_swarm_task_containers_are_skipped():
    body = SCRIPT.read_text()
    assert "com.docker.swarm.task.id" in body


def test_waits_for_overlay_before_deciding():
    body = SCRIPT.read_text()
    # Decisions must be made against steady state, not mid-race.
    assert 'docker network inspect "$NET"' in body
    assert body.index("network inspect") < body.index("RestartPolicy.Name")


def test_no_container_gets_an_unconditional_start():
    """The traefik special case is gone with the swarm conversion.

    It started catena-traefik whenever it was not running, with no error
    evidence at all -- justified while traefik was a plain container that
    could sit Exited forever. A swarm service re-dispatches itself, and the
    swarm-task skip above already excludes it, so the rule now has no subject.
    Leaving it would be a start-anything-named-X path with no evidence gate.
    """
    # Comment lines stripped: the header explains why the special case went,
    # and naming it there must not read as the thing still being there.
    body = "\n".join(
        line for line in SCRIPT.read_text().splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "CATENA_TRAEFIK_CTR" not in body
    assert "catena-traefik" not in body
    assert "$CTR" not in body
    assert "not running; starting" not in body
    # Every start is now downstream of the error gate.
    assert body.index(".State.Error") < body.index("docker start")


def test_role_installs_the_artifacts():
    assert SCRIPT.exists() and UNIT.exists()
    copy = _find("drop network-nudge script")["ansible.builtin.copy"]
    assert copy["src"].endswith("catena-network-nudge.sh")
    assert copy["dest"] == "/usr/local/bin/catena-network-nudge"
    tmpl = _find("render network-nudge systemd unit")["ansible.builtin.template"]
    assert tmpl["src"] == "catena-network-nudge.service.j2"
    assert tmpl["dest"] == "/etc/systemd/system/catena-network-nudge.service"


def test_the_nudge_is_owned_by_roles_docker_not_roles_traefik():
    """The race is between dockerd's container restore and the swarm init.
    roles/traefik has no plain container left and no relationship to it."""
    traefik_tasks = (ROOT / "roles" / "traefik" / "tasks" / "main.yml").read_text()
    assert "catena-network-nudge.service.j2" not in traefik_tasks
    assert not (ROOT / "roles" / "traefik" / "templates"
                / "catena-network-nudge.service.j2").exists()


def test_timeout_reaches_the_script_on_both_paths():
    # The drop-in dispatches the SCRIPT (not the unit), so it needs its own
    # env; the unit carries the same values for the manual diagnostic path.
    dropin = DROPIN.read_text()
    assert ("--setenv=CATENA_NETWORK_NUDGE_TIMEOUT={{ docker_network_nudge_timeout }}"
            in dropin)
    assert "--setenv=CATENA_NETWORK={{ catena_network_name }}" in dropin
    unit = UNIT.read_text()
    assert ("Environment=CATENA_NETWORK_NUDGE_TIMEOUT={{ docker_network_nudge_timeout }}"
            in unit)
    assert "CATENA_NETWORK_NUDGE_TIMEOUT" in SCRIPT.read_text()
