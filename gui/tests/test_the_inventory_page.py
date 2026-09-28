"""The installer starts on the inventory, and saves every page into it.

The first page lists the inventories under ansible/inventory/ and creates new
ones; each step's answers are written to that inventory's `.env` as the client
advances. These drive the real HTTP handler on loopback, with the probes
stubbed so nothing leaves the machine.

Run: uv run pytest tests/test_the_inventory_page.py
"""
from __future__ import annotations

import http.client
import http.server
import threading
import urllib.parse

import pytest

from catena_gui import registry, run as run_mod, server, steps as steps_mod

DOC = registry.load()


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A running handler over an empty ansible/inventory/ in tmp_path."""
    monkeypatch.setattr(steps_mod, "validate", lambda *a, **k: [])
    ansible = tmp_path / "ansible"
    (ansible / "inventory" / "example").mkdir(parents=True)
    server._Handler.run = None
    server._Handler.doc = DOC
    server._Handler.ansible_dir = ansible
    server._Handler.inventory_root = ansible / "inventory"
    server._Handler.all_steps = steps_mod.build(DOC)
    server._Handler.last_checks = {}
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), server._Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    def request(method, path, form=None):
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1])
        body = urllib.parse.urlencode(form or {})
        conn.request(method, path, body=body if form is not None else None,
                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        resp = conn.getresponse()
        return resp.status, resp.getheader("Location"), resp.read().decode()

    yield request, ansible / "inventory"
    httpd.shutdown()


def _first_field_of(step: str) -> steps_mod.Field:
    built = next(s for s in steps_mod.build(DOC) if s.name == step)
    return next(f for f in built.fields if not f.secret and not f.options)


def test_nothing_opens_before_an_inventory_is_chosen(client):
    request, _ = client
    assert request("GET", "/")[1] == "/inventory"
    first = steps_mod.build(DOC)[0].name
    assert request("GET", f"/step/{first}")[1] == "/inventory"


def test_creating_an_inventory_saves_it_and_opens_the_first_step(client):
    request, root = client
    status, where, _ = request("POST", "/inventory", {"create": "newco"})
    assert status == 303
    assert where == f"/step/{steps_mod.build(DOC)[0].name}"
    assert (root / "newco" / run_mod.ENV_FILENAME).is_file()
    _, _, page = request("GET", "/inventory")
    assert "newco" in page


def test_a_bad_name_is_refused_on_the_page(client):
    request, root = client
    status, where, _ = request("POST", "/inventory", {"create": "Bad Name"})
    assert (status, where) == (303, "/inventory")
    assert "lower-case" in request("GET", "/inventory")[2]
    assert run_mod.inventories(root) == []


def test_an_example_is_shown_and_never_filled_in(client):
    """A pre-filled example is an answer nobody gave."""
    request, _ = client
    request("POST", "/inventory", {"create": "newco"})
    field = next(f for s in steps_mod.build(DOC) for f in s.fields if f.example)
    step = next(s.name for s in steps_mod.build(DOC) if field in s.fields)
    page = request("GET", f"/step/{step}")[2]
    assert f'placeholder="{field.example}"' in page
    assert f'name="{field.key}" value=""' in page


def test_a_page_saves_into_the_inventory_and_an_opened_one_reads_it(client):
    request, root = client
    request("POST", "/inventory", {"create": "newco"})
    step = steps_mod.build(DOC)[0].name
    field = _first_field_of(step)
    request("POST", f"/step/{step}", {field.key: "edited-value"})
    seed = run_mod._seed()
    assert seed.read_existing_env(root / "newco" / ".env")[field.key] == "edited-value"

    request("POST", "/inventory", {"create": "other"})
    request("POST", "/inventory", {"open": "newco"})
    assert 'value="edited-value"' in request("GET", f"/step/{step}")[2]


def test_the_install_output_reaches_no_file(tmp_path, monkeypatch):
    """`catena-cli install` ends by printing the passwords it shows once. The
    page shows them from memory; no file in the inventory, or anywhere the
    launcher writes, keeps them."""
    import sys

    monkeypatch.setattr(server.render, "install_command", lambda *a: [
        sys.executable, "-c", "print('restic-password-SHOWN-ONCE')"])
    current = run_mod.load(tmp_path / "clientco", run_mod.secret_keys_from(DOC))
    server.start_install(current, tmp_path).join(timeout=30)
    assert current.state == run_mod.STATE_DONE
    assert "restic-password-SHOWN-ONCE" in list(current.log)
    for path in (tmp_path / "clientco").rglob("*"):
        if path.is_file():
            assert "SHOWN-ONCE" not in path.read_text(errors="replace"), path


def test_a_credential_typed_on_a_page_reaches_no_file(client):
    request, root = client
    request("POST", "/inventory", {"create": "newco"})
    secret = next(f for s in steps_mod.build(DOC) for f in s.fields if f.secret)
    step = next(s.name for s in steps_mod.build(DOC) if secret in s.fields)
    request("POST", f"/step/{step}", {secret.key: "do-not-write-me"})
    for path in (root / "newco").rglob("*"):
        if path.is_file():
            assert "do-not-write-me" not in path.read_text(errors="replace"), path
