"""The container engine is pinned, held, and moved only by the engine upgrade.

An operator converge runs `apt upgrade: dist` (bootstrap/roles/common), which
moved Docker to whatever shipped that day with no health check behind it, and
a host that converges itself never moved it at all. The engine is now a pin:
a first install gets it, apt holds it, and catena-admin's catena-engine-upgrade
reads the same two keys out of the panel image's tree to move an installed
host, checking the host came back.

Run: uv run pytest tests/unit/test_docker_engine_pin.py
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
ROLE = ANSIBLE / "bootstrap" / "roles" / "docker"


def _defaults() -> dict:
    return yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())


def _tasks() -> list[dict]:
    return yaml.safe_load((ROLE / "tasks" / "main.yml").read_text())


def _task(name: str) -> dict:
    return next(t for t in _tasks() if t.get("name") == name)


def test_the_pins_are_plain_upstream_versions():
    """catena-engine-upgrade reads these two keys as literals and builds the
    apt strings itself; a Jinja expression here would reach it unrendered."""
    d = _defaults()
    assert re.fullmatch(r"\d+\.\d+\.\d+", d["docker_version"]), d["docker_version"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", d["docker_containerd_version"])


def test_the_engine_packages_are_the_pins_in_dockers_apt_naming():
    d = _defaults()
    assert d["docker_engine_packages"] == [
        "docker-ce=5:{{ docker_version }}{{ docker_apt_version_suffix }}",
        "docker-ce-cli=5:{{ docker_version }}{{ docker_apt_version_suffix }}",
        "containerd.io={{ docker_containerd_version }}{{ docker_apt_version_suffix }}",
    ]
    assert d["docker_apt_version_suffix"].startswith("-1~debian.")


def test_an_installed_engine_is_left_where_it_is():
    """Only a host with no engine gets the pin from a converge; moving an
    installed one is catena-engine-upgrade's, behind its health check."""
    install = _task("Install the pinned container engine")
    assert install["ansible.builtin.apt"]["name"] == "{{ docker_engine_packages }}"
    assert "install ok installed" in install["when"]


def test_the_engine_is_held_after_every_install_path():
    names = [t.get("name") for t in _tasks()]
    hold = _task("Hold the container engine packages")
    assert hold["ansible.builtin.dpkg_selections"]["selection"] == "hold"
    assert hold["loop"] == "{{ catena_held_engine_packages }}"
    assert names.index("Hold the container engine packages") > names.index(
        "Reinstall docker packages cleanly after purge")


def test_the_held_list_is_the_engine_trio_and_uninstall_releases_it():
    shared = yaml.safe_load(
        (ANSIBLE / "playbooks" / "group_vars" / "all" / "main.yml").read_text())
    assert shared["catena_held_engine_packages"] == [
        "docker-ce", "docker-ce-cli", "containerd.io"]
    uninstall = (ANSIBLE / "playbooks" / "uninstall.yml").read_text()
    assert "['apt-mark', 'unhold'] + catena_held_engine_packages" in uninstall


def test_renovate_proposes_the_pins_after_fourteen_days_by_hand():
    cfg = json.loads((ANSIBLE.parent / "renovate.json").read_text())
    tracked = {m["depNameTemplate"]: m for m in cfg["customManagers"]}
    assert "docker_version" in tracked["docker-engine"]["matchStrings"][0]
    assert "docker_containerd_version" in tracked["containerd"]["matchStrings"][0]
    rules = cfg["packageRules"]
    age = next(r for r in rules if "minimumReleaseAge" in r)
    assert set(age["matchDepNames"]) == {"docker-engine", "containerd"}
    assert age["minimumReleaseAge"] == "14 days" and age["automerge"] is False
    held_back = next(r for r in rules if r.get("enabled") is False)
    assert held_back["matchDepNames"] == ["containerd"]
    assert set(held_back["matchUpdateTypes"]) == {"minor", "major"}
