"""SECRETS.md names every secret the store's registries declare.

The registries in helpers/onbox_config.py decide what the store holds;
SECRETS.md is where a reader looks it up. A secret added to a registry and
left out of the document is a store key nobody can find the purpose of.

Run: uv run pytest tests/unit/test_secrets_md_lists_the_store.py
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ANSIBLE = Path(__file__).resolve().parents[2]
SECRETS_MD = ANSIBLE / "SECRETS.md"


@pytest.fixture(scope="module")
def oc():
    spec = importlib.util.spec_from_file_location(
        "onbox_config", ANSIBLE / "helpers" / "onbox_config.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_every_declared_secret_is_named(oc):
    body = SECRETS_MD.read_text()
    missing = [k for k in oc.secret_names() if f"`{k}`" not in body]
    assert not missing, f"SECRETS.md does not name: {missing}"


def test_every_named_internal_secret_is_declared(oc):
    """The category-2 list holds only what the loader mints."""
    body = SECRETS_MD.read_text()
    section = body.split("### 2.", 1)[1].split("### 3.", 1)[0]
    listed = {
        line.split("`")[1] for line in section.splitlines()
        if line.startswith("- `")
    }
    listed.discard("oauth2_proxy_cookie_secret_<zone>")
    assert listed == set(oc.INTERNAL_SECRETS)
