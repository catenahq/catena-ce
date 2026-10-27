"""docker_registry_mirror_url takes one URL or several separated by commas, and the
docker role writes them to daemon.json in order.

The role's daemon.json task renders here through Ansible's templar, with the
task's own vars, so the file's bytes are the ones a host gets.

Run: uv run pytest tests/unit/test_docker_registry_mirrors.py
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template

ANSIBLE = Path(__file__).resolve().parents[2]
ROLE = ANSIBLE / "bootstrap" / "roles" / "docker"
TASK = "Drop /etc/docker/daemon.json"

BASE = {"docker_data_root": "/srv/catena/docker", "docker_log_max_size": "50m",
        "docker_log_max_files": 5, "docker_live_restore": False}


def _trusted(value):
    """The role's vars as Ansible loads them from the task file: every string a template."""
    if isinstance(value, str):
        return trust_as_template(value)
    if isinstance(value, dict):
        return {k: _trusted(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_trusted(v) for v in value]
    return value


def _daemon_json(mirror_url: str) -> str:
    task = next(t for t in yaml.safe_load((ROLE / "tasks" / "main.yml").read_text())
                if t.get("name") == TASK)
    variables = {**BASE, **_trusted(task["vars"]), "docker_registry_mirror_url": mirror_url}
    return Templar(loader=DataLoader(), variables=variables).template(
        trust_as_template(task["ansible.builtin.copy"]["content"]))


def _config(mirror_url: str) -> dict:
    return json.loads(_daemon_json(mirror_url))


def test_no_mirror_writes_neither_key():
    cfg = _config("")
    assert "registry-mirrors" not in cfg and "insecure-registries" not in cfg


def test_blank_entries_are_no_mirror():
    cfg = _config(" , ")
    assert "registry-mirrors" not in cfg and "insecure-registries" not in cfg


def test_one_http_url_renders_as_it_always_did():
    assert _daemon_json("http://10.0.0.1:5000") == (
        '{\n'
        '    "data-root": "/srv/catena/docker",\n'
        '    "insecure-registries": [\n'
        '        "10.0.0.1:5000"\n'
        '    ],\n'
        '    "live-restore": false,\n'
        '    "log-driver": "json-file",\n'
        '    "log-opts": {\n'
        '        "max-file": "5",\n'
        '        "max-size": "50m"\n'
        '    },\n'
        '    "registry-mirrors": [\n'
        '        "http://10.0.0.1:5000"\n'
        '    ]\n'
        '}\n')


def test_one_https_url_has_no_insecure_registry():
    cfg = _config("https://mirror.example:5000")
    assert cfg["registry-mirrors"] == ["https://mirror.example:5000"]
    assert cfg["insecure-registries"] == []


def test_two_urls_keep_their_order_and_only_the_http_one_is_insecure():
    cfg = _config("https://10.0.0.1:5001, http://10.0.0.1:5000")
    assert cfg["registry-mirrors"] == ["https://10.0.0.1:5001", "http://10.0.0.1:5000"]
    assert cfg["insecure-registries"] == ["10.0.0.1:5000"]


def test_spaces_around_commas_and_a_trailing_comma_are_ignored():
    cfg = _config("  http://a:1 ,http://b:2 ,")
    assert cfg["registry-mirrors"] == ["http://a:1", "http://b:2"]
    assert cfg["insecure-registries"] == ["a:1", "b:2"]
