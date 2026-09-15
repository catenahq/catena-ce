"""Lock the catena-network-nudge boot-time self-heal.

`catena-network` is a swarm-scoped overlay. On a docker.service start
dockerd restores restart-policy containers during "Loading containers",
which runs BEFORE the swarm node re-materializes the overlay -- and the
boot-time restore gets exactly ONE attempt per container, so whatever loses
that race stays Exited with RestartCount=0 forever. Swarm services
re-dispatch themselves; the Portainer compose containers (keycloak-server-1,
nextcloud-talk-hpb-1, ...) have nothing to heal them, so a plain VPS reboot
can strand Keycloak until the next converge. Observed live: an overlay that
appeared 2.5s after "Loading containers: done".

Those compose containers are what the nudge protects, which is why it lives
in bootstrap/roles/docker -- the race is between dockerd's container restore and the
swarm init, both owned by that role.

What must hold:
  - the heal is gated on the EXACT overlay-not-found error, so a container
    the operator stopped on purpose is never started behind their back
    (the blanket-reap mistake, twice reverted);
  - swarm task containers are skipped -- swarm owns their lifecycle;
  - NO container gets an unconditional start-if-not-running: every start is
    downstream of the error gate;
  - the traefik-named artifacts stay removed, else a stale drop-in fires a
    second script alongside this one.

Run: uv run pytest tests/unit/test_network_nudge_heals_stranded.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3] / "ansible"
SCRIPT = ROOT / "scripts" / "catena-network-nudge.sh"
TASKS = ROOT / "bootstrap" / "roles" / "docker" / "tasks" / "main.yml"
UNIT = ROOT / "bootstrap" / "roles" / "docker" / "templates" / "catena-network-nudge.service.j2"
DROPIN = ROOT / "bootstrap" / "roles" / "docker" / "templates" / "docker-service-nudge-dropin.conf.j2"


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
    """No container is started on not-running alone.

    A rule that starts one by name, with no recorded error, is a
    start-anything-named-X path with no evidence gate. It also has no subject:
    catena-traefik is a swarm service, the task manager re-dispatches it, and
    the swarm-task skip above excludes it from this loop anyway.
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
    reconcile/roles/traefik has no plain container left and no relationship to it."""
    traefik_tasks = (ROOT / "reconcile" / "roles" / "traefik" / "tasks" / "main.yml").read_text()
    assert "catena-network-nudge.service.j2" not in traefik_tasks
    assert not (ROOT / "reconcile" / "roles" / "traefik" / "templates"
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
