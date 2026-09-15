"""A converge must never clear an activated licence.

The token lives in the on-box store: `catena-admin/license/onbox.go` reads
`secrets.catena_license`, with the container environment as a bench-only
fallback. A token reaching the shell ONLY through that environment makes
Portainer's stack Env its de-facto home, leaving `merge_env_portainer`'s
preserve rule as the only thing between a CE re-converge and a dropped
activation -- and catena-admin is deployed against the local swarm, not through
the Portainer stack API at all.

That only holds while the store side stays fill-only. These tests pin the two
properties it rests on: the store owns the key, and nothing in the converge
path can overwrite it with a blank.

Run: uv run pytest tests/unit/test_license_state_in_store.py
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ANSIBLE = Path(__file__).resolve().parents[2]
LOADER = ANSIBLE / "playbooks" / "tasks" / "load_onbox_config.yml"


@pytest.fixture(scope="module")
def oc():
    spec = importlib.util.spec_from_file_location(
        "onbox_config", ANSIBLE / "helpers" / "onbox_config.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_licence_is_a_store_owned_external_secret(oc):
    """External, not internal: catena never mints one. Being in the registry
    at all is what makes the store its home rather than a compose file."""
    assert "catena_license" in oc.EXTERNAL_SECRETS
    assert "catena_license" not in oc.INTERNAL_SECRETS
    assert "catena_license" not in oc.USER_HELD_SECRETS


def test_an_absent_inventory_value_cannot_clear_a_stored_licence(oc):
    """The converge captures in-scope variables and adopts them. On a host
    with no licence in its inventory that capture is blank, and a blank that
    overwrote would de-activate a paying client on their next converge."""
    store = {"secrets": {"catena_license": "eyJhbGciOi.REAL.TOKEN"}, "config": {}}
    oc.adopt(store, {"catena_license": ""})
    assert store["secrets"]["catena_license"] == "eyJhbGciOi.REAL.TOKEN"
    oc.adopt(store, {"catena_license": None})
    assert store["secrets"]["catena_license"] == "eyJhbGciOi.REAL.TOKEN"


def test_a_different_inventory_value_cannot_replace_a_stored_licence(oc):
    """Fill-only, not last-write-wins: the store is the owner once set."""
    store = {"secrets": {"catena_license": "REAL"}, "config": {}}
    oc.adopt(store, {"catena_license": "SOMETHING-ELSE"})
    assert store["secrets"]["catena_license"] == "REAL"


def test_the_licence_seeds_into_an_empty_store(oc):
    """Fill-only still has to FILL, or `catena recover` could not restore an
    activation from the client's saved token."""
    store = {"secrets": {}, "config": {}}
    oc.adopt(store, {"catena_license": "RESTORED"})
    assert store["secrets"]["catena_license"] == "RESTORED"


def test_the_converge_loader_never_adopts_with_overwrite():
    """The one caller that would break every property above. --overwrite is a
    deliberate settings-page action (apply_inputs), never a converge."""
    body = LOADER.read_text()
    assert "--adopt-file" in body
    assert "--overwrite" not in body, (
        "the converge loader must adopt fill-only: with --overwrite a blank "
        "capture would clear the stored licence on the next converge"
    )
