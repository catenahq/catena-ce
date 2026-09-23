"""helpers/knobs.yml is the registry, and the rendered JSON says the same thing.

Three properties, and the third is the one with teeth.

The first two are about the artifact: the YAML parses and validates, and
knobs.json is what rendering it produces. A stale JSON is a knob the store and
the panel never learn about, so `render_knobs.py --check` failing here is the
same signal CI gives.

The third holds the registry against the declarations it is replacing. Until
every consumer reads the registry, both copies exist, and a transcription error
between them is invisible: the store would accept a key the panel never shows,
or the `.env` would carry a default the registry does not know about. These
assertions fail while the two disagree and get deleted one at a time as each
consumer moves over.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ANSIBLE_DIR = Path(__file__).resolve().parents[2]
if str(ANSIBLE_DIR) not in sys.path:
    sys.path.insert(0, str(ANSIBLE_DIR))

from helpers import onbox_config, render_knobs  # noqa: E402

ENV_TEMPLATE = ANSIBLE_DIR / "inventory" / "example" / ".env.example"


@pytest.fixture(scope="module")
def registry() -> dict:
    return json.loads(render_knobs.RENDERED.read_text())


def test_source_validates():
    """Every shape rule in render_knobs.load() holds on the shipped file."""
    render_knobs.load()


def test_rendered_json_is_current():
    assert render_knobs.main(["--check"]) == 0, (
        "knobs.json is stale -- run `python3 helpers/render_knobs.py --write`"
    )


def test_secrets_match_the_external_allowlist(registry):
    """The registry's secrets ARE onbox_config's EXTERNAL_SECRETS.

    That set is the allowlist apply_inputs enforces, so a credential in one and
    not the other is either a key the client can be told to enter and the store
    refuses, or a key the store accepts that nothing asks for.
    """
    assert {s["key"] for s in registry["secrets"]} == set(onbox_config.EXTERNAL_SECRETS)


def test_store_projections_match_settings_config(registry):
    """Store-resident knobs that carry a `var` are exactly SETTINGS_CONFIG.

    A store key WITHOUT a var is legitimate and deliberately not in that dict:
    a runtime lane reads it straight off the store with no converge in between,
    so there is no Ansible fact to publish it as.
    """
    projected = {
        k["key"]: k["var"]
        for k in registry["config"]
        if k["residence"] == "store" and "var" in k
    }
    assert projected == dict(onbox_config.SETTINGS_CONFIG)


def test_controller_knobs_match_bootstrap_config(registry):
    controller = {k["key"] for k in registry["config"] if k["residence"] == "controller"}
    assert controller == set(onbox_config.BOOTSTRAP_CONFIG)


def test_every_knob_has_exactly_one_owner(registry):
    """No key belongs to two residences, and none belongs to none.

    onbox_config states this property about its own two sets ("a key that is in
    NEITHER set is a gate failure rather than an unnoticed third owner"), but
    the host identity keys were in neither: they are written into hosts.yml by
    seed, which is a third owner that nothing named. `residence: host` names it.
    """
    for knob in registry["config"]:
        assert knob["residence"] in render_knobs.RESIDENCES, knob["key"]


def test_env_template_keys_are_declared(registry):
    """Every KEY= line in the shipped template is a knob that declares an env
    default, and every such knob appears in the template.

    This is what lets seed stop parsing the template for its key list: once the
    generated file IS the registry's output, the two cannot disagree. Until
    then, they must not.
    """
    declared = {k["key"]: k["env"]["default"] for k in registry["config"] if "env" in k}

    in_template: dict[str, str] = {}
    for raw in ENV_TEMPLATE.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        in_template[key.strip()] = value.strip()

    assert set(in_template) == set(declared), (
        "template and registry disagree about WHICH keys are in the .env"
    )
    assert in_template == declared, (
        "template and registry disagree about a DEFAULT"
    )
