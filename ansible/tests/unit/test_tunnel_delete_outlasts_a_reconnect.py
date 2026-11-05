"""A Domain apply deletes the old tunnel whatever cloudflared does meanwhile.

cloudflared keeps running on the host while cloudflare_tunnel_regenerate
deletes the tunnel it serves, and Cloudflare refuses to delete a tunnel with
live connections. The role deletes with cascade=true, which drops the
connections and the tunnel in one call, and retries a refused delete; one
refused every time stops the run with a sentence naming the call and its HTTP
status, and never the API token.

The role's own delete and stop tasks run here under ansible-playbook against a
local stand-in for the Cloudflare API, with the delay between tries set to 0.

Run: uv run pytest tests/unit/test_tunnel_delete_outlasts_a_reconnect.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

from ansible_tree import role_dir, task_index

TASKS = role_dir("cloudflare_tunnel_regenerate") / "tasks" / "main.yml"
DELETE = "delete existing tunnel(s)"
STOP = "stop when Cloudflare kept an old tunnel"
TOKEN = "cf-test-api-token-value"
REFUSED = {"success": False, "errors": [
    {"code": 1022, "message": "This tunnel has active connections"}]}


def _role_tasks(*needles: str) -> list[dict]:
    tasks = yaml.safe_load(TASKS.read_text(encoding="utf-8"))
    return [dict(tasks[task_index(tasks, n)]) for n in needles]


def delete_tries() -> int:
    """The first try and its retries."""
    return int(_role_tasks(DELETE)[0]["retries"]) + 1


def test_the_retries_are_bounded_to_about_half_a_minute():
    delete = _role_tasks(DELETE)[0]
    assert 2 <= int(delete["retries"]) * int(delete["delay"]) <= 60


class _CloudflareStandIn(ThreadingHTTPServer):
    """Answers each DELETE with the next status scripted for its tunnel."""

    def __init__(self, script: dict[str, list[int]]):
        self.script = script
        self.calls: list[tuple[str, str]] = []
        super().__init__(("127.0.0.1", 0), _Handler)


class _Handler(BaseHTTPRequestHandler):
    def do_DELETE(self):  # noqa: N802
        server: _CloudflareStandIn = self.server  # type: ignore[assignment]
        server.calls.append((self.path, self.headers.get("Authorization", "")))
        tunnel = self.path.split("?")[0].rsplit("/", 1)[-1]
        answers = server.script[tunnel]
        status = answers.pop(0) if len(answers) > 1 else answers[0]
        body = json.dumps(REFUSED if status == 400 else {"success": True, "result": {}}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


def _run(tmp_path: Path, script: dict[str, list[int]]) -> tuple[subprocess.CompletedProcess, list]:
    delete, stop = _role_tasks(DELETE, STOP)
    delete["delay"] = 0
    server = _CloudflareStandIn({k: list(v) for k, v in script.items()})
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        play = [{
            "hosts": "localhost",
            "gather_facts": False,
            "vars": {
                "cloudflare_api_base": f"http://127.0.0.1:{server.server_address[1]}/client/v4",
                "_cf_regen_account_id": "acct",
                "_cf_regen_api_token": TOKEN,
                "_cf_regen_list": {"json": {"result": [{"id": t} for t in script]}},
            },
            "tasks": [delete, stop],
        }]
        playbook = tmp_path / "play.yml"
        playbook.write_text(yaml.safe_dump(play), encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
        # ansible-playbook refuses to start without a UTF-8 locale.
        env.update(LC_ALL="C.UTF-8", LANG="C.UTF-8",
                   ANSIBLE_HOME=str(tmp_path / ".ansible"), ANSIBLE_NOCOLOR="1",
                   ANSIBLE_RETRY_FILES_ENABLED="0", NO_PROXY="127.0.0.1")
        result = subprocess.run(
            [sys.executable, "-m", "ansible.cli.playbook", "-i", "localhost,",
             "-e", f"ansible_connection=local ansible_python_interpreter={sys.executable}",
             str(playbook)],
            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=240)
    finally:
        server.shutdown()
        server.server_close()
    return result, server.calls


def test_a_delete_refused_once_is_tried_again_and_ends_ok(tmp_path):
    result, calls = _run(tmp_path, {"tunnel-a": [400, 200], "tunnel-b": [200]})
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-2000:]
    paths = [p for p, _ in calls]
    assert paths.count("/client/v4/accounts/acct/cfd_tunnel/tunnel-a?cascade=true") == 2
    assert paths.count("/client/v4/accounts/acct/cfd_tunnel/tunnel-b?cascade=true") == 1
    assert all(auth == f"Bearer {TOKEN}" for _, auth in calls)


def test_a_tunnel_already_gone_counts_as_deleted(tmp_path):
    result, calls = _run(tmp_path, {"tunnel-a": [404]})
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-2000:]
    assert len(calls) == 1


def test_a_delete_refused_every_time_stops_with_the_call_and_status(tmp_path):
    result, calls = _run(tmp_path, {"tunnel-a": [400]})
    out = result.stdout + result.stderr
    assert result.returncode != 0
    assert len(calls) > 1, "a refused delete was not tried again"
    assert "Cloudflare refused to delete the old tunnel" in out, out[-3000:]
    assert len(calls) == delete_tries()
    assert "DELETE cfd_tunnel/tunnel-a?cascade=true answered HTTP 400 on every try" in out, out[-3000:]
    assert TOKEN not in out
    assert "active connections" not in out, "the stop printed Cloudflare's answer body"
