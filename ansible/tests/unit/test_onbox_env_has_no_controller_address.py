"""A file a host-local service reads must not carry the controller's address.

`portainer_api_base` is `http://{{ ansible_host }}:.../api` -- where the
CONTROLLER reaches the box. It is not stable: it flips between the public and
tailnet address across converges and differs again after a restore onto other
infrastructure. Rendering it into an env file that a unit on the host reads
gives that unit an address for the machine it is already running on, and the
value goes stale the moment the address moves:

    catena-dashboard-sync.service: FAILED
    request failed on GET /stacks: <urlopen error [Errno 113] No route to
    host>          (the env said 10.139.244.121; the host was .110)

dashboard-sync renders the Traefik route files for client apps, so a stale
base means every app deployed afterwards gets no route and answers 404. Bench
run 2026-08-31T14-59-08-adca reported that as "traefik did not route
'book.11003300.xyz'", with the app healthy at 1/1 and its vps.route.host
label correct -- three layers from the cause.

It is the same reason these files are not idempotent: an address that changes
between converges rewrites the file every time.

Run: uv run pytest tests/unit/test_onbox_env_has_no_controller_address.py
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

# Word-bounded: `catena_stack_update_portainer_api_base` is the correct
# indirection (it resolves to the on-box base) and merely CONTAINS the name.
# `_` is a word character, so \b does not match inside it.
_CONTROLLER_BASE = re.compile(r"\bportainer_api_base\b")
_ANSIBLE_HOST = re.compile(r"\bansible_host\b")

_ANSIBLE = Path(__file__).resolve().parents[2]

# Templates rendered into files that a unit ON THE HOST reads.
_ONBOX_ENV_TEMPLATES = (
    "reconcile/roles/infrastructure/templates/dashboard-sync.env.j2",
    "bootstrap/roles/catena_admin_host/templates/stack-update.env.j2",
)


def test_the_onbox_base_is_loopback():
    main = yaml.safe_load(
        (_ANSIBLE / "playbooks/group_vars/all/main.yml").read_text())
    assert "127.0.0.1" in main["portainer_api_base_onbox"]
    # And the controller-facing one still resolves through ansible_host --
    # the uri tasks run from the controller and need a reachable address.
    assert "ansible_host" in main["portainer_api_base"]


@pytest.mark.parametrize("rel", _ONBOX_ENV_TEMPLATES)
def test_onbox_env_does_not_reference_the_controller_base(rel):
    text = (_ANSIBLE / rel).read_text()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert not _CONTROLLER_BASE.search(stripped), (
            f"{rel} renders the controller-facing portainer_api_base into a "
            f"file read on the host; use portainer_api_base_onbox"
        )
        assert not _ANSIBLE_HOST.search(stripped), (
            f"{rel} renders ansible_host into a file read on the host; that "
            f"address is the controller's view and is not stable"
        )


# A template whose OUTPUT is read by the controller, or shown to a person, may
# legitimately name the address the controller uses. Each entry carries the
# reason, so adding one is a decision rather than a quiet edit to the gate.
_CONTROLLER_FACING_TEMPLATES: dict[str, str] = {
    "reconcile/roles/backup/templates/backup.env.j2": (
        "BACKUP_SCP_HINT_HOST is printed in the export script's scp hint and "
        "nothing dials it. The operator needs an address reachable FROM "
        "OUTSIDE, which is the one thing an on-box name cannot give: a "
        "tunnel-fronted host publishes no address of its own, so the "
        "controller's view is the only candidate that exists at render time. "
        "Residual, accepted: the hint prints the address the last converge "
        "used, so it can name one the box no longer answers on. It misleads a "
        "human for one command; it does not break a machine path."
    ),
}


def test_no_role_template_renders_the_controller_address():
    """The rule, rather than the two files that broke first.

    d60d017 fixed dashboard-sync.env.j2 and stack-update.env.j2 and gated those
    two by name. 279fff2 then found admin-ssh-config.j2 doing the same thing and
    named three more places still to check. Every one of those was found from
    the outside, by a bench run, days apart, one at a time -- which is what a
    gate scoped to the known offenders buys.

    Templates under roles/*/templates/ are rendered onto the host, so unless a
    template is listed above as controller-facing, an ansible_host in it is the
    same defect: a file on the box holding the controller's view of where the
    box is, which flips public-IP <-> tailnet-IP across converges and differs
    again after a restore onto other infrastructure.
    """
    offenders = []
    for path in sorted((_ANSIBLE / "roles").rglob("templates/**/*.j2")):
        rel = str(path.relative_to(_ANSIBLE))
        if rel in _CONTROLLER_FACING_TEMPLATES:
            continue
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if _ANSIBLE_HOST.search(stripped) or _CONTROLLER_BASE.search(stripped):
                offenders.append(f"{rel}:{number}: {stripped}")
    assert not offenders, (
        "these render the controller's address into a file on the host:\n  "
        + "\n  ".join(offenders)
        + "\nUse a stable on-box name (127.0.0.1, host.docker.internal, "
          "portainer_api_base_onbox), or add the template to "
          "_CONTROLLER_FACING_TEMPLATES with the reason it is an exception."
    )


def test_the_admin_known_hosts_scan_does_not_depend_on_a_reachable_address():
    """ssh-keyscan runs ON the box, so loopback addresses the sshd whose key it
    wants. Aimed at the box's own public-or-tailnet address it asks for the same
    key over a route that can fail -- and it exits 0 with empty stdout when it
    cannot reach what it is given, so an unreachable address writes an EMPTY
    known_hosts and every panel action then fails host key verification, naming
    nothing."""
    body = (_ANSIBLE / "bootstrap/roles/catena_admin_host/tasks/main.yml").read_text()
    start = body.index("- name: \"SSH dir: scan this host's own key")
    scan = body[start:body.index("\n- name:", start + 1)]
    assert "127.0.0.1" in scan, "the scan must go over loopback"
    assert not _ANSIBLE_HOST.search(scan), (
        "the scan must not depend on the controller's view of this host"
    )
    assert "failed_when" in scan, (
        "ssh-keyscan exits 0 on failure; without failed_when an empty result "
        "writes an empty known_hosts and is discovered from a failed dispatch"
    )
