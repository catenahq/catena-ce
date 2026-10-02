"""The installer starts on the inventory, then asks everything on one page.

The first page lists the inventories under ansible/inventory/, one per line,
and creates new ones. The install page holds every section in one form; each
section's Check saves the whole form into that inventory's `.env` and shows its
results under its own button, and Install checks every section first. These
drive the real HTTP handler on loopback, with the probes stubbed so nothing
leaves the machine.

Run: uv run pytest tests/test_the_inventory_page.py
"""
from __future__ import annotations

import http.client
import http.server
import json
import re
import sys
import threading
import urllib.parse

import pytest

from catena_gui import registry, run as run_mod, server, steps as steps_mod

DOC = registry.load()
STEPS = steps_mod.build(DOC)
# The real probe dispatcher, kept before the fixture stubs it.
VALIDATE = steps_mod.validate


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
    server._Handler.all_steps = STEPS
    server._Handler.last_checks = {}
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), server._Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    def request(method, path, form=None):
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1])
        body = urllib.parse.urlencode(form or {}, doseq=True)
        conn.request(method, path, body=body if form is not None else None,
                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        resp = conn.getresponse()
        return resp.status, resp.getheader("Location"), resp.read().decode()

    yield request, ansible / "inventory"
    httpd.shutdown()


_REQUIRED = {"HOST_PUBLIC_IP": "203.0.113.10", "ADMIN_EMAIL": "admin@client.test"}


def _section(page: str, name: str) -> str:
    match = re.search(rf'<section id="{name}">(.*?)</section>', page, re.S)
    assert match, f"no section {name!r} on the page"
    return match.group(1)


def test_nothing_opens_before_an_inventory_is_chosen(client):
    request, _ = client
    assert request("GET", "/")[1] == "/inventory"
    assert request("POST", "/", {"check": STEPS[0].name})[1] == "/inventory"


def test_creating_an_inventory_saves_it_and_opens_the_install_page(client):
    request, root = client
    status, where, _ = request("POST", "/inventory", {"create": "newco"})
    assert (status, where) == (303, "/")
    assert (root / "newco" / run_mod.ENV_FILENAME).is_file()
    page = request("GET", "/")[2]
    for step in STEPS:
        assert f'<section id="{step.name}">' in page, step.name


def test_the_inventories_are_listed_one_per_line(client):
    request, _ = client
    for name in ("alpha", "beta", "gamma"):
        request("POST", "/inventory", {"create": name})
    page = request("GET", "/inventory")[2]
    rows = re.findall(r"<li><button type=submit name=open value=\"(\w+)\">", page)
    assert rows == ["alpha", "beta", "gamma"]


def test_opening_an_inventory_writes_nothing(client):
    """The `.env` is a file its client may have edited: it is written by a
    Check, never by being opened."""
    request, root = client
    (root / "handmade").mkdir()
    env = root / "handmade" / ".env"
    env.write_text("# my notes\nHOST_PUBLIC_IP=198.51.100.7\n")
    request("POST", "/inventory", {"open": "handmade"})
    assert env.read_text() == "# my notes\nHOST_PUBLIC_IP=198.51.100.7\n"
    assert not (root / "handmade" / run_mod.STATE_FILENAME).exists()


def test_a_check_keeps_a_key_the_template_does_not_carry(client):
    request, root = client
    (root / "handmade").mkdir()
    env = root / "handmade" / ".env"
    env.write_text("HOST_PUBLIC_IP=198.51.100.7\nCLIENT_OWN_KEY=kept\n")
    request("POST", "/inventory", {"open": "handmade"})
    request("POST", "/", {"check": STEPS[0].name})
    saved = run_mod._seed().read_existing_env(env)
    assert (saved["CLIENT_OWN_KEY"], saved["HOST_PUBLIC_IP"]) == ("kept", "198.51.100.7")


def test_a_bad_name_is_refused_on_the_page(client):
    request, root = client
    status, where, _ = request("POST", "/inventory", {"create": "Bad Name"})
    assert (status, where) == (303, "/inventory")
    assert "lower-case" in request("GET", "/inventory")[2]
    assert run_mod.inventories(root) == []


def test_a_field_explains_itself_in_a_tooltip(client):
    """The name, `(optional)` and a `(?)`; the explanation is in the tooltip,
    not on the page."""
    request, _ = client
    request("POST", "/inventory", {"create": "newco"})
    field = next(f for s in STEPS for f in s.fields if f.doc and f.optional)
    page = request("GET", "/")[2]
    head = re.search(rf'<div class=field data-key="{field.key}"[^>]*>'
                     r"<div class=head>(.*?)</div>", page, re.S).group(1)
    assert f">{field.key}</label>" in head
    assert "(optional)" in head
    assert "(?)<span class=tt role=tooltip>" in head


def test_an_example_is_shown_and_never_filled_in(client):
    """A pre-filled example is an answer nobody gave."""
    request, _ = client
    request("POST", "/inventory", {"create": "newco"})
    field = next(f for s in STEPS for f in s.fields if f.example)
    page = request("GET", "/")[2]
    assert f'placeholder="{field.example}"' in page
    assert f'name="{field.key}" value=""' in page


def test_checking_one_section_saves_the_whole_form(client):
    """One form: a check keeps everything typed, the acknowledgement further
    down included, and reopening the inventory shows all of it again."""
    request, root = client
    request("POST", "/inventory", {"create": "newco"})
    status, where, _ = request("POST", "/", {
        **_REQUIRED, "ack": "yes", "check": STEPS[0].name})
    assert (status, where) == (303, f"/#{STEPS[0].name}")
    saved = run_mod._seed().read_existing_env(root / "newco" / ".env")
    assert saved["ADMIN_EMAIL"] == "admin@client.test"

    request("POST", "/inventory", {"create": "other"})
    request("POST", "/inventory", {"open": "newco"})
    page = request("GET", "/")[2]
    assert 'value="admin@client.test"' in page
    assert "name=ack value=yes checked" in page


def test_a_check_shows_under_its_own_button(client, monkeypatch):
    request, _ = client
    request("POST", "/inventory", {"create": "newco"})
    monkeypatch.setattr(steps_mod, "validate", lambda step, a, s: [
        steps_mod.Check(f"probe of {step}", True)])
    request("POST", "/", {**_REQUIRED, "check": "target"})
    page = request("GET", "/")[2]
    target = _section(page, "target")
    assert target.index("probe of target") > target.index(">Check</button>")
    assert "probe of" not in _section(page, "keyset")


def test_install_checks_every_section_and_stops_on_the_first_that_fails(
        client, monkeypatch):
    request, _ = client
    request("POST", "/inventory", {"create": "newco"})
    started = []
    monkeypatch.setattr(server, "start_install", lambda *a: started.append(a))
    seen = []

    def validate(step, answers, secrets):
        seen.append(step)
        return [steps_mod.Check("reachable", step != "target")]

    monkeypatch.setattr(steps_mod, "validate", validate)
    status, where, _ = request("POST", "/", {"install": "yes", "ack": "yes",
                                             **_REQUIRED})
    assert (status, where) == (303, "/#target")
    assert seen == [s.name for s in STEPS]
    assert started == []


def test_install_starts_when_every_section_passes(client, monkeypatch):
    request, _ = client
    request("POST", "/inventory", {"create": "newco"})
    started = []
    monkeypatch.setattr(server, "start_install", lambda *a: started.append(a))
    status, where, _ = request("POST", "/", {"install": "yes", "ack": "yes",
                                             **_REQUIRED})
    assert (status, where) == (303, "/#install")
    assert len(started) == 1


def test_the_ticked_box_is_the_acknowledgement_install_reads(client, monkeypatch):
    """The real keyset probe, through the handler: Install stops on the keyset
    without the box, and starts with it."""
    request, _ = client
    request("POST", "/inventory", {"create": "newco"})
    started = []
    monkeypatch.setattr(server, "start_install", lambda *a: started.append(a))
    monkeypatch.setattr(steps_mod, "validate", VALIDATE)
    monkeypatch.setitem(steps_mod.PROBES, "target", lambda a, s: [])
    status, where, _ = request("POST", "/", {"install": "yes", **_REQUIRED})
    assert (status, where) == (303, "/#keyset")
    assert started == []
    status, where, _ = request("POST", "/", {"install": "yes", "ack": "yes",
                                             **_REQUIRED})
    assert (status, where) == (303, "/#install")
    assert len(started) == 1


def test_a_required_field_left_empty_stops_the_install_on_its_section(
        client, monkeypatch):
    """No probe runs on a section whose required answers are missing, and
    Install does not start."""
    request, _ = client
    request("POST", "/inventory", {"create": "newco"})
    started = []
    monkeypatch.setattr(server, "start_install", lambda *a: started.append(a))
    status, where, _ = request("POST", "/", {"install": "yes", "ack": "yes"})
    assert (status, where) == (303, f"/#{STEPS[0].name}")
    assert started == []
    section = _section(request("GET", "/")[2], STEPS[0].name)
    assert "is required" in section


def test_the_page_asks_for_the_server_and_nothing_the_panel_holds(client):
    """The domain, the private network and the backups are entered in the
    panel once the server runs; the installer has no field for any of them."""
    request, _ = client
    request("POST", "/inventory", {"create": "newco"})
    page = request("GET", "/")[2]
    asked = set(re.findall(r'<div class=field data-key="(\w+)"', page))
    held = {e["key"] for e in [*DOC["secrets"], *DOC["config"]] if e.get("panel")}
    assert asked and not (asked & held), asked & held


def test_the_access_page_names_the_ssh_forward(client):
    request, _ = client
    request("POST", "/inventory", {"create": "newco"})
    request("POST", "/", {"HOST_PUBLIC_IP": "203.0.113.10", "check": "target"})
    page = request("GET", "/access")[2]
    assert "Reaching the panel" in page
    assert "panel@203.0.113.10" in page
    assert "Settings" in page


def test_the_acknowledgement_box_comes_before_its_text(client):
    request, _ = client
    request("POST", "/inventory", {"create": "newco"})
    last = _section(request("GET", "/")[2], STEPS[-1].name)
    assert re.search(r"<label class=ack><input type=checkbox name=ack value=yes>"
                     r"<span>", last)
    assert "name=install" in last


def test_suggestions_are_offered_beside_a_free_text_field():
    """The key the provider installed is usually one of the pairs already on
    this machine, and sometimes it is not: a list to pick from, and a field
    that takes any path."""
    field = steps_mod.Field(key="SSH_PRIVATE_KEY", secret=False, optional=False,
                            options=[], default="", doc="",
                            suggestions=["~/.ssh/id_ed25519"])
    html = server._field_html(field, "~/.ssh/catena_ed25519")
    assert 'list="s-SSH_PRIVATE_KEY"' in html
    assert '<datalist id="s-SSH_PRIVATE_KEY"><option value="~/.ssh/id_ed25519">' in html
    assert 'value="~/.ssh/catena_ed25519"' in html


def test_the_passwords_block_is_held_apart_from_the_log(tmp_path, monkeypatch):
    """The block the CLI frames goes to the run's keyset, which the page shows
    on its own; every other line, its colour codes stripped, to the log."""
    cli = registry.ansible_module("catena_cli")
    script = "\n".join((
        "print('\\x1b[1;34m== Stage: bootstrap\\x1b[0m')",
        f"print({cli.KEYSET_BEGIN!r})",
        "print('Admin password: pw')",
        f"print({cli.KEYSET_END!r})",
        "print('converge')"))
    monkeypatch.setattr(server.render, "install_command",
                        lambda *a: [sys.executable, "-c", script])
    current = run_mod.load(tmp_path / "clientco", run_mod.secret_keys_from(DOC))
    server.start_install(current, tmp_path).join(timeout=30)
    assert current.keyset == "Admin password: pw"
    assert list(current.log) == ["== Stage: bootstrap", "converge"]


def test_the_install_reads_no_input(tmp_path, monkeypatch):
    """Nothing on the page can answer a question: the install's stdin is empty
    and no terminal, so a prompt takes its default instead of waiting."""
    monkeypatch.setattr(server.render, "install_command", lambda *a: [
        sys.executable, "-c",
        "import sys; print(sys.stdin.isatty(), repr(sys.stdin.read()))"])
    current = run_mod.load(tmp_path / "clientco", run_mod.secret_keys_from(DOC))
    server.start_install(current, tmp_path).join(timeout=30)
    assert list(current.log) == ["False ''"]


def test_the_passwords_show_on_their_own_and_the_output_scrolls(client):
    """The passwords section waits hidden until the install prints them. The
    output is a scrolling box the page polls, not a page that reloads."""
    request, _ = client
    request("POST", "/inventory", {"create": "newco"})
    current = server._Handler.run
    current.state = run_mod.STATE_INSTALLING
    page = request("GET", "/")[2]
    assert "<section id=keyset hidden>" in page
    assert '<pre id=log class=log data-state="installing">' in page
    assert "http-equiv=refresh" not in page

    current.keyset = "Admin password: pw"
    current.log.append("converge")
    page = request("GET", "/")[2]
    keyset = re.search(r"<section id=keyset>(.*?)</section>", page, re.S)
    assert keyset and "Admin password: pw" in keyset.group(1)
    status, _, body = request("GET", "/progress")
    assert status == 200
    assert json.loads(body) == {"state": "installing", "log": "converge",
                                "keyset": "Admin password: pw"}


def test_the_install_output_reaches_no_file(tmp_path, monkeypatch):
    """`catena-cli install` prints the passwords it shows once. The page shows
    them from memory; no file in the inventory, or anywhere the launcher
    writes, keeps them."""
    monkeypatch.setattr(server.render, "install_command", lambda *a: [
        sys.executable, "-c", "print('restic-password-SHOWN-ONCE')"])
    current = run_mod.load(tmp_path / "clientco", run_mod.secret_keys_from(DOC))
    server.start_install(current, tmp_path).join(timeout=30)
    assert current.state == run_mod.STATE_DONE
    assert "restic-password-SHOWN-ONCE" in list(current.log)
    for path in (tmp_path / "clientco").rglob("*"):
        if path.is_file():
            assert "SHOWN-ONCE" not in path.read_text(errors="replace"), path


def test_a_credential_typed_on_the_page_reaches_no_file(client):
    request, root = client
    request("POST", "/inventory", {"create": "newco"})
    secret = next(f for s in STEPS for f in s.fields if f.secret)
    request("POST", "/", {secret.key: "do-not-write-me", "check": STEPS[0].name})
    for path in (root / "newco").rglob("*"):
        if path.is_file():
            assert "do-not-write-me" not in path.read_text(errors="replace"), path
