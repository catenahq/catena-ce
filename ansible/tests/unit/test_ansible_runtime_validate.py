"""bootstrap/roles/ansible_runtime checks its venv in tasks/validate.yml, which
main.yml includes after installing and playbooks/validate.yml runs again: the
version check lives once, and the venv is only root's to change.

Run: uv run pytest tests/unit/test_ansible_runtime_validate.py
"""
from __future__ import annotations

import yaml

from ansible_tree import role_dir, walk_tasks

ROLE = role_dir("ansible_runtime")
MAIN = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text())
VALIDATE = yaml.safe_load((ROLE / "tasks" / "validate.yml").read_text())


def _version_asserts(tasks: list[dict]) -> list[dict]:
    return [t for t in walk_tasks(tasks)
            if "ansible.builtin.assert" in t
            and "ansible_runtime_minimum" in str(t["ansible.builtin.assert"])]


def test_the_version_check_is_in_validate_once():
    assert len(_version_asserts(VALIDATE)) == 1
    probes = [t for t in walk_tasks(VALIDATE) if "ansible-playbook" in str(t)]
    assert len(probes) == 1


def test_main_includes_validate_and_holds_no_copy_of_the_check():
    assert not _version_asserts(MAIN)
    assert "ansible-playbook" not in str(MAIN)
    includes = [t["ansible.builtin.include_tasks"] for t in MAIN
                if "ansible.builtin.include_tasks" in t]
    assert "validate.yml" in includes


def test_the_include_comes_after_the_install():
    names = [str(t.get("name")) for t in MAIN]
    install = next(i for i, n in enumerate(names) if "install" in n)
    include = next(i for i, t in enumerate(MAIN) if "ansible.builtin.include_tasks" in t)
    assert install < include


def test_validate_asserts_the_venv_is_root_0755():
    task = next(t for t in VALIDATE if "ansible.builtin.include_role" in t)
    assert task["ansible.builtin.include_role"] == {"name": "common", "tasks_from": "_assert_path_perms"}
    paths = {p["path"]: p for p in task["vars"]["_assert_paths"]}
    assert set(paths) == {"{{ ansible_runtime_dir }}", "{{ ansible_runtime_dir }}/bin"}
    for entry in paths.values():
        assert (entry["type"], entry["owner"], entry["group"], entry["mode"]) == (
            "directory", "root", "root", "0755")
