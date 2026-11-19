"""Run the shipped rotation and validation against account-scoped Cloudflare state."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

from ansible_tree import role_dir

TASKS = role_dir("cloudflare_tunnel_regenerate") / "tasks"


class _Cloudflare(ThreadingHTTPServer):
    def __init__(self, same_account=False, kept="valid"):
        self.accounts = {"new.example": "new", "old.example": "new" if same_account else "old",
                         "extra.example": "extra"}
        self.tokens = {zone: f"test-only-{zone}" for zone in self.accounts}
        self.tunnels = {"new": [{"id": "current"}], "old": [{"id": "outgoing"}],
                        "extra": [{"id": "preserved"}]}
        self.calls = []
        # "revoked": the outgoing token sees no zone; "dns_only": it sees the
        # zone but not the account's tunnels.
        self.kept = kept
        super().__init__(("127.0.0.1", 0), _Handler)

    def denied(self, zone, *, account_call):
        return zone == "old.example" and (
            self.kept == "revoked" or (self.kept == "dns_only" and account_call))


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self._answer()

    def do_DELETE(self):  # noqa: N802
        self._answer()

    def _answer(self):
        server = self.server
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        server.calls.append((self.command, self.path))
        auth = self.headers.get("Authorization")
        status = 200
        result = []
        if url.path == "/zones":
            zone = query["name"][0]
            if auth != f"Bearer {server.tokens[zone]}" or server.denied(zone, account_call=False):
                status = 403
            else:
                result = [{"account": {"id": server.accounts[zone]}}]
        else:
            account = url.path.split("/")[2]
            allowed = {f"Bearer {server.tokens[z]}" for z, a in server.accounts.items()
                       if a == account and not server.denied(z, account_call=True)}
            if auth not in allowed:
                status = 403
            elif self.command == "GET":
                assert query["is_deleted"] == ["false"]
                assert query["name"][0].startswith("test-prefix-")
                result = server.tunnels[account]
            else:
                assert query == {"cascade": ["true"]}
                tunnel = url.path.rsplit("/", 1)[-1]
                server.tunnels[account] = [t for t in server.tunnels[account] if t["id"] != tunnel]
        body = json.dumps({"success": status == 200, "result": result}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


def _run(tmp_path, server, *, validate=False):
    store = {"config": {"CLOUDFLARE_ZONE": "new.example",
                        "CLOUDFLARE_ZONES": [{"zone": "old.example"}, {"zone": "extra.example"}],
                        "CLOUDFLARED_TUNNEL_NAME_PREFIX": "test-prefix-"},
             "secrets": {"cloudflare_api_token": server.tokens["new.example"],
                         "cloudflare_api_tokens": server.tokens}}
    store_path = tmp_path / "config.json"
    store_path.write_text(json.dumps(store))
    source = "validate.yml" if validate else "main.yml"
    tasks = yaml.safe_load((TASKS / source).read_text())
    # The engine handoff is outside the role's account cleanup boundary.
    tasks = [t for t in tasks if "ansible.builtin.include_role" not in t]
    for task in tasks:
        if "ansible.builtin.include_tasks" in task:
            task["ansible.builtin.include_tasks"] = str(TASKS / task["ansible.builtin.include_tasks"])
    play = [{"hosts": "localhost", "gather_facts": False,
             "vars": {"catena_onbox_config_path": str(store_path),
                      "cloudflare_api_base": f"http://127.0.0.1:{server.server_port}"},
             "tasks": tasks}]
    playbook = tmp_path / "play.yml"
    playbook.write_text(yaml.safe_dump(play))
    env = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
    env.update(LC_ALL="C.UTF-8", LANG="C.UTF-8", ANSIBLE_HOME=str(tmp_path / ".ansible"),
               ANSIBLE_NOCOLOR="1", ANSIBLE_RETRY_FILES_ENABLED="0", NO_PROXY="127.0.0.1")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        result = subprocess.run(
            [sys.executable, "-m", "ansible.cli.playbook", "-i", "localhost,",
             "-e", f"ansible_connection=local ansible_python_interpreter={sys.executable}",
             str(playbook)], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=240)
    finally:
        server.shutdown()
        server.server_close()
    output = result.stdout + result.stderr
    assert all(token not in output for token in server.tokens.values())
    assert json.loads(store_path.read_text()) == store, "token retirement belongs to the handoff"
    return result.returncode, output


@pytest.mark.parametrize("same_account", [False, True])
def test_rotation_deletes_outgoing_tunnels_with_their_own_token(tmp_path, same_account):
    server = _Cloudflare(same_account=same_account)
    code, output = _run(tmp_path, server)
    assert code == 0, output[-4000:]
    assert server.tunnels["new"] == []
    if not same_account:
        assert server.tunnels["old"] == []
    assert server.tunnels["extra"] == [{"id": "preserved"}]
    assert not any("extra" in path for _, path in server.calls)
    assert any("name=old.example" in path for _, path in server.calls)


@pytest.mark.parametrize("leftover", [False, True])
def test_validation_refuses_a_tunnel_left_in_the_outgoing_account(tmp_path, leftover):
    server = _Cloudflare()
    if not leftover:
        server.tunnels["old"] = []
    code, output = _run(tmp_path, server, validate=True)
    assert (code != 0) == leftover, output[-4000:]
    if leftover:
        assert "Cloudflare still holds this host's tunnel" in output
    assert all(method == "GET" for method, _ in server.calls)


def test_validation_accepts_the_replacement_in_a_shared_account(tmp_path):
    code, output = _run(tmp_path, _Cloudflare(same_account=True), validate=True)
    assert code == 0, output[-4000:]


@pytest.mark.parametrize("kept", ["revoked", "dns_only"])
@pytest.mark.parametrize("validate", [False, True])
def test_an_outgoing_token_that_cannot_reach_its_account_is_reported(tmp_path, kept, validate):
    server = _Cloudflare(kept=kept)
    code, output = _run(tmp_path, server, validate=validate)
    assert code == 0, output[-4000:]
    assert "cannot list this host's tunnels" in output
    assert server.tunnels["old"] == [{"id": "outgoing"}]
    assert server.tunnels["new"] == ([{"id": "current"}] if validate else [])
