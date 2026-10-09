"""Every master-realm sign-in the converge makes reads the admin password on
stdin: no kcadm login and no keycloak-config-cli import puts it in an argv or
in a task environment, which Ansible prefixes to its module's `/bin/sh -c`
command line, both shown by `ps` to every local user (CV7).

kcadm reads KC_CLI_PASSWORD when no --password is given
(BaseConfigCredentialsCmd, Keycloak 26.6.7), and `docker exec -e NAME` and
`docker run -e NAME` forward the variable from the client's environment.

Run: uv run pytest tests/unit/test_keycloak_logins_read_the_password_on_stdin.py
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
TASKS = ANSIBLE / "reconcile" / "roles" / "keycloak" / "tasks"


def _walk(tasks):
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        yield task
        for key in ("block", "rescue", "always"):
            yield from _walk(task.get(key))


def _command(task: dict) -> tuple[str, dict]:
    for key in ("ansible.builtin.command", "ansible.builtin.shell"):
        mod = task.get(key)
        if mod is None:
            continue
        if isinstance(mod, str):
            return mod, task.get("args") or {}
        text = " ".join(map(str, mod["argv"])) if "argv" in mod else str(mod.get("cmd", ""))
        return text, {**mod, **(task.get("args") or {})}
    return "", {}


def _signins():
    for path in sorted(TASKS.glob("*.yml")):
        for task in _walk(yaml.safe_load(path.read_text())):
            text, args = _command(task)
            if "config credentials" in text or "keycloak-config-cli" in text \
                    or "keycloak_config_cli_image" in text:
                yield path.name, task, text, args


def test_there_are_sign_ins_to_check():
    names = {(name, task["name"]) for name, task, _, _ in _signins()}
    assert len(names) >= 5, names


def test_no_sign_in_carries_the_password_but_on_stdin():
    for name, task, text, args in _signins():
        where = f"{name}: {task['name']!r}"
        assert "admin_password" not in text and "--password" not in text, where
        assert "environment" not in task, where
        assert args.get("stdin") == "{{ admin_password }}", where
        assert task.get("no_log") is True, where


def test_the_wrapper_hands_the_line_to_the_command_it_runs():
    argv = yaml.safe_load((TASKS / "_config_cli_import.yml").read_text())[0][
        "ansible.builtin.command"]["argv"]
    assert argv[:2] == ["bash", "-c"] and "-e" in argv and "KEYCLOAK_PASSWORD" in argv
    out = subprocess.run(argv[:4] + ["printenv", "KEYCLOAK_PASSWORD"],
                         input="s3cret=pw\n", text=True, capture_output=True, check=True)
    assert out.stdout == "s3cret=pw\n"
