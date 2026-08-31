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

It is the same reason these files were never idempotent: an address that
changes between converges rewrites the file every time.

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
    "roles/infrastructure/templates/dashboard-sync.env.j2",
    "roles/catena-admin/templates/stack-update.env.j2",
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
