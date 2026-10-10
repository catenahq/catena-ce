"""The tailnet join and the Lockdown's reachability check keep their
credentials out of every process's arguments: `tailscale up` reads its auth key
from a root-only file (--auth-key=file:), and the check reads the OAuth secret
and the Headscale API key on stdin. An argv, or a task environment, which
Ansible prefixes to the command line it runs, is what `ps` shows every local
user (CV7).

Run: uv run pytest tests/unit/test_tailnet_credentials_stay_off_the_command_line.py
"""
from __future__ import annotations

import io
import json
import re
import sys
from pathlib import Path

import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
if str(ANSIBLE) not in sys.path:
    sys.path.insert(0, str(ANSIBLE))

from helpers import tailnet_reachability as tr  # noqa: E402

ROLE_TASKS = ANSIBLE / "bootstrap" / "roles" / "tailscale" / "tasks"
HELPER = ANSIBLE / "helpers" / "tailnet_reachability.py"
# Every file whose tasks hold a tailnet credential: the role, and the two plays
# that run it.
TAILNET_FILES = (*sorted(ROLE_TASKS.glob("*.yml")),
                 ANSIBLE / "playbooks" / "lockdown.yml",
                 ANSIBLE / "playbooks" / "rotate-tailscale.yml")
# The stored credentials and every value minted from them.
SECRETS = {"tailscale_oauth_client_secret", "headscale_api_key",
           "headscale_preauth_key", "ts_oauth_token", "ts_minted_key",
           "hs_minted_key", "_hs_authkey"}
_COMMANDS = ("ansible.builtin.command", "ansible.builtin.shell",
             "ansible.builtin.script", "ansible.builtin.raw")
_UPS = ("tailscale up with minted key (SaaS)",
        "Headscale: tailscale up via --login-server")


def _flatten(node) -> list[dict]:
    """Every task, descending into plays, block / rescue / always."""
    out: list[dict] = []
    if isinstance(node, list):
        for item in node:
            out += _flatten(item)
    elif isinstance(node, dict):
        out.append(node)
        for key in ("block", "rescue", "always", "pre_tasks", "tasks", "post_tasks"):
            if key in node:
                out += _flatten(node[key])
    return out


def _tasks(path: Path) -> list[dict]:
    return _flatten(yaml.safe_load(path.read_text(encoding="utf-8")))


def _task(path: Path, name: str) -> dict:
    return next(t for t in _tasks(path) if t.get("name") == name)


def _command_text(task: dict) -> str | None:
    for key in _COMMANDS:
        mod = task.get(key)
        if mod is None:
            continue
        if isinstance(mod, str):
            return mod
        return " ".join(map(str, mod["argv"])) if "argv" in mod else str(mod.get("cmd", ""))
    return None


def _vars_in(text: str) -> set[str]:
    return set(re.findall(r"\b([a-z_][a-z0-9_]*)\b", " ".join(re.findall(r"{{(.*?)}}", text))))


def test_no_tailnet_task_puts_a_credential_in_a_command_line():
    for path in TAILNET_FILES:
        for task in _tasks(path):
            assert "environment" not in task, (
                f"{path.name}: {task.get('name')!r} sets an environment")
            text = _command_text(task)
            if text is not None:
                assert not (_vars_in(text) & SECRETS), (
                    f"{path.name}: {task.get('name')!r} renders a credential "
                    f"into its command line: {text}")


def test_tailscale_up_reads_its_key_from_the_file():
    main = ROLE_TASKS / "main.yml"
    for name in _UPS:
        argv = _task(main, name)["ansible.builtin.command"]["argv"]
        assert "--auth-key=file:{{ _ts_key_dir.path }}/authkey" in argv, argv
        assert not any(a.startswith("--authkey") for a in argv), argv


def test_the_key_file_is_root_only_and_removed_whatever_the_join_does():
    main = ROLE_TASKS / "main.yml"
    auth = _task(main, "Authenticate the node")
    names = [t.get("name") for t in auth["block"]]
    made = _task(main, "Create a root-only directory for the auth key")
    assert made["ansible.builtin.tempfile"]["state"] == "directory"
    assert made["register"] == "_ts_key_dir"
    write = _task(main, "Write the auth key into it")
    copy = write["ansible.builtin.copy"]
    assert copy["dest"] == "{{ _ts_key_dir.path }}/authkey"
    assert (copy["owner"], copy["mode"]) == ("root", "0600")
    assert {"ts_minted_key", "_hs_authkey"} <= _vars_in(copy["content"])
    assert write.get("no_log") is True, "the key's write is logged"
    assert names.index(made["name"]) < names.index(write["name"]) < min(
        names.index(n) for n in _UPS)
    [remove] = auth["always"]
    assert remove["ansible.builtin.file"] == {"path": "{{ _ts_key_dir.path }}",
                                              "state": "absent"}


def test_the_reachability_check_reads_its_input_on_stdin():
    reachable = ROLE_TASKS / "reachable.yml"
    drop = _task(reachable, "Tailnet: drop the reachability check to /root")
    copy = drop["ansible.builtin.copy"]
    assert copy["src"].endswith("/helpers/tailnet_reachability.py")
    assert copy["mode"] == "0700"
    ask = _task(reachable, "Tailnet: ask the control server and a peer whether "
                           "this host is reachable")
    mod = ask["ansible.builtin.command"]
    assert mod["argv"] == ["python3", copy["dest"]]
    assert mod["stdin"] == "{{ _ts_reachable_input | to_json }}"
    assert ask.get("no_log") is True, "the check's stdin is logged"
    sent = set(ask["vars"]["_ts_reachable_input"])
    read = set(re.findall(r'cfg\.get\("([A-Z_]+)"\)', HELPER.read_text(encoding="utf-8")))
    assert sent == read, (sent, read)


def test_the_helper_takes_its_input_from_stdin_alone(monkeypatch, capsys):
    assert "os.environ" not in HELPER.read_text(encoding="utf-8")
    cfg = {"TAILNET_PROVIDER": "tailscale",
           "TAILSCALE_OAUTH_CLIENT_SECRET": "tskey-client-SECRET"}
    seen = []
    monkeypatch.setattr(tr, "check", lambda c: seen.append(c) or {"ok": True})
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(cfg) + "\n"))
    assert tr.main() == 0
    assert seen == [cfg]
    assert json.loads(capsys.readouterr().out) == {"ok": True}
