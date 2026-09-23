"""helpers/knobs.yml is the registry, and the rendered JSON says the same thing.

Three properties, and the third is the one with teeth.

The first two are about the artifact: the YAML parses and validates, and
knobs.json is what rendering it produces. A stale JSON is a knob the store and
the panel never learn about, so `render_knobs.py --check` failing here is the
same signal CI gives.

The third holds what onbox_config DERIVES from the registry: which credentials
the store accepts, which knobs reach the converge as facts, and that the three
residences partition rather than overlap. Plus the two failure modes of reading
a file instead of holding a literal -- an absent registry has to raise rather
than empty, and an explicit path has to win.

The `.env` template is still hand-written, so the last assertion holds it
against the registry's defaults. It goes when the template is generated.
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


def test_the_store_accepts_every_declared_credential(registry):
    """EXTERNAL_SECRETS is the allowlist apply_inputs enforces, and it is the
    registry's secrets. A credential in one and not the other is either a key
    the client can be told to enter and the store refuses, or a key the store
    accepts that nothing asks for."""
    assert {s["key"] for s in registry["secrets"]} == set(onbox_config.EXTERNAL_SECRETS)


def test_only_projected_store_knobs_reach_the_converge(registry):
    """A stored knob with no declared `var` stays out of SETTINGS_CONFIG.

    Those are read straight off the store by a runtime lane with no converge in
    between, so there is no fact to publish. Publishing one anyway would create
    an Ansible variable no role reads, which is the silent half of the drift
    this registry exists to stop.
    """
    unprojected = {
        k["key"] for k in registry["config"]
        if k["residence"] == "store" and "var" not in k
    }
    assert unprojected, "the fixture is meaningless if every store knob projects"
    assert not (unprojected & set(onbox_config.SETTINGS_CONFIG))


def test_residences_do_not_overlap(registry):
    """The three residences partition the config knobs.

    A key in two of them would be read from two places, and the `.env` copy
    would quietly win on a tag-scoped converge that skipped the store loader --
    which is the failure the two-owner split was drawn to prevent.
    """
    store = set(onbox_config.SETTINGS_CONFIG)
    controller = set(onbox_config.BOOTSTRAP_CONFIG)
    host = {k["key"] for k in registry["config"] if k["residence"] == "host"}
    assert not (store & controller)
    assert not (store & host)
    assert not (controller & host)


def test_a_missing_registry_raises_rather_than_emptying(monkeypatch, tmp_path):
    """An install that lost the file is broken, and this is the only place that
    can say so.

    Falling back to empty sets would leave apply_inputs refusing every
    credential the client supplies and the converge publishing no config facts,
    both silent and both indistinguishable from a host nobody configured yet.
    """
    monkeypatch.setenv("CATENA_KNOBS", str(tmp_path / "absent.json"))
    monkeypatch.setenv("CATENA_PAYLOAD_LIB", str(tmp_path))
    monkeypatch.setattr(onbox_config, "__file__", str(tmp_path / "onbox_config.py"))
    with pytest.raises(FileNotFoundError):
        onbox_config._knobs_path()


def test_an_explicit_path_wins(tmp_path, monkeypatch):
    """CATENA_KNOBS is how a test or an odd host layout points at a registry."""
    target = tmp_path / "elsewhere.json"
    target.write_text("{}")
    monkeypatch.setenv("CATENA_KNOBS", str(target))
    assert onbox_config._knobs_path() == target


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
