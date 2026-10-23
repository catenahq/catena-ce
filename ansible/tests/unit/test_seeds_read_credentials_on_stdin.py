"""The Healthchecks and Beszel seeds read their credentials on stdin: none is
in a process's argv or environment during a converge, where `ps` shows it to
every local user (CV7).

Run: uv run pytest tests/unit/test_seeds_read_credentials_on_stdin.py
"""
from __future__ import annotations

import io
import json
import re
from pathlib import Path

import yaml

from healthchecks_seed_standins import SEED as HC_SEED, BASE_CONFIG, install_fake_django

ANSIBLE = Path(__file__).resolve().parents[2]
TASKS = ANSIBLE / "reconcile" / "roles" / "infrastructure" / "tasks"
BESZEL_SEED = ANSIBLE / "scripts" / "beszel-seed.py"

# (task file, the fact holding the seed's values, the seed tasks reading it)
SEEDS = (
    ("healthchecks.yml", "_healthchecks_seed_config",
     ("run the Django seed script",)),
    ("beszel.yml", "_beszel_seed_config",
     ("seed the universal token", "default alert rules")),
)


def _tasks(name: str) -> list[dict]:
    return [t for t in yaml.safe_load((TASKS / name).read_text()) if isinstance(t, dict)]


def _task(tasks: list[dict], fragment: str) -> dict:
    return next(t for t in tasks if fragment in str(t.get("name", "")))


def _config_fact(tasks: list[dict], fact: str) -> dict:
    return next(t["ansible.builtin.set_fact"][fact] for t in tasks
                if fact in (t.get("ansible.builtin.set_fact") or {}))


def _command(task: dict) -> tuple[str, dict]:
    for key in ("ansible.builtin.command", "ansible.builtin.shell"):
        mod = task.get(key)
        if mod is None:
            continue
        if isinstance(mod, str):
            return mod, task.get("args") or {}
        args = {**mod, **(task.get("args") or {})}
        text = " ".join(map(str, mod["argv"])) if "argv" in mod else str(mod.get("cmd", ""))
        return text, args
    raise AssertionError(f"{task.get('name')!r} runs no command")


def _vars_in(template: str) -> set[str]:
    return set(re.findall(r"\b([a-z_][a-z0-9_]*)\b", " ".join(re.findall(r"{{(.*?)}}", template))))


def test_each_seed_task_takes_its_values_on_stdin_alone():
    for file, fact, fragments in SEEDS:
        tasks = _tasks(file)
        config = _config_fact(tasks, fact)
        secret_vars = set().union(*(_vars_in(str(v)) for v in config.values())) - {
            "default", "string", "bool", "lower", "inventory_hostname"}
        for fragment in fragments:
            task = _task(tasks, fragment)
            text, args = _command(task)
            assert "environment" not in task, f"{file}: {fragment!r} sets an environment"
            assert args.get("stdin") == "{{ " + fact + " | to_json }}", (
                f"{file}: {fragment!r} does not read {fact} on stdin")
            assert fact not in text and not (_vars_in(text) & secret_vars), (
                f"{file}: {fragment!r} renders a seed value into its command line")
            assert task.get("no_log") is True, f"{file}: {fragment!r} logs its result"
        assert next(t for t in tasks if fact in (t.get("ansible.builtin.set_fact") or {})).get(
            "no_log") is True, f"{file}: the {fact} fact is logged"


def test_the_healthchecks_exec_passes_no_env_flag():
    text, _ = _command(_task(_tasks("healthchecks.yml"), "run the Django seed script"))
    exec_line = text[text.index("docker exec"):]
    assert not re.search(r"\s(-e|--env)\b", exec_line), exec_line


def test_the_scripts_read_no_environment():
    for script in (HC_SEED, BESZEL_SEED):
        assert "os.environ" not in script.read_text(), script.name


def test_the_healthchecks_config_keys_are_the_ones_the_script_reads():
    config = _config_fact(_tasks("healthchecks.yml"), "_healthchecks_seed_config")
    read = set(re.findall(r'seed_config(?:\[|\.get\()"([a-z_]+)"', HC_SEED.read_text()))
    assert set(config) == read == set(BASE_CONFIG)


def test_the_beszel_config_keys_are_the_ones_the_script_reads():
    config = _config_fact(_tasks("beszel.yml"), "_beszel_seed_config")
    body = BESZEL_SEED.read_text()
    main = body[body.index("def main()"):body.index("\ndef ", body.index("def main()"))]
    read = set(re.findall(r'cfg(?:\[|\.get\()"([a-z_]+)"', main))
    # smtp_app_name keeps the script's default.
    assert set(config) == read - {"smtp_app_name"}


def test_the_shell_line_hands_the_first_stdin_line_to_the_script(monkeypatch):
    """The task's stdin (the values) followed by the script, as `cat -` joins
    them, run through the task's own `manage.py shell -c` code."""
    text, _ = _command(_task(_tasks("healthchecks.yml"), "run the Django seed script"))
    assert "cat - /root/catena-healthchecks-seed.py" in text
    [code] = re.findall(r"manage\.py shell\s*\\?\s*-c '([^']*)'", text)
    managers = install_fake_django(monkeypatch)
    stdin = json.dumps({**BASE_CONFIG, "admin_email": "seeded@example.com"}) + "\n" + HC_SEED.read_text()
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    exec(code, {"__name__": "__shell__"})
    [operator] = managers["user"].rows
    assert operator.email == "seeded@example.com"
