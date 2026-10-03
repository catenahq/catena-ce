"""The installer has two tabs: the inventory, then one installation page.

These drive the real HTTP handler on loopback, with the probes stubbed so
nothing leaves the machine.

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
from html import escape

import pytest

from catena_gui import i18n, registry, run as run_mod, server, steps as steps_mod

DOC = registry.load()
STEPS = steps_mod.build(DOC)


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A running handler over an empty ansible/inventory/ in tmp_path."""
    monkeypatch.setattr(steps_mod, "validate", lambda *a, **k: [])
    ansible = tmp_path / "ansible"
    (ansible / "inventory").mkdir(parents=True)
    server._Handler.run = None
    server._Handler.doc = DOC
    server._Handler.ansible_dir = ansible
    server._Handler.inventory_root = ansible / "inventory"
    server._Handler.all_steps = STEPS
    server._Handler.last_checks = []
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), server._Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    def request(method, path, form=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1])
        body = urllib.parse.urlencode(form or {}, doseq=True)
        conn.request(method, path, body=body if form is not None else None,
                     headers={"Content-Type": "application/x-www-form-urlencoded",
                              **(headers or {})})
        resp = conn.getresponse()
        request.cookie = resp.getheader("Set-Cookie")
        return resp.status, resp.getheader("Location"), resp.read().decode()

    yield request, ansible / "inventory"
    httpd.shutdown()


@pytest.fixture
def started(monkeypatch):
    """The installs the page started, recorded rather than run."""
    calls: list[tuple] = []
    monkeypatch.setattr(server, "start_install", lambda *a: calls.append(a))
    return calls


_REQUIRED = {"HOST_PUBLIC_IP": "203.0.113.10", "ADMIN_EMAIL": "admin@client.test"}
_INSTALL = {"install": "yes"}


def _section(page: str, name: str) -> str:
    match = re.search(rf'<section id="?{name}"?>(.*?)</section>', page, re.S)
    assert match, f"no section {name!r} on the page"
    return match.group(1)


def test_nothing_opens_before_an_inventory_is_chosen(client):
    request, _ = client
    assert request("GET", "/")[1] == "/inventory"
    assert request("POST", "/", _INSTALL)[1] == "/inventory"


def test_the_tabs_are_inventory_and_installation(client):
    request, _ = client
    assert "Installation" not in request("GET", "/inventory")[2]
    request("POST", "/inventory", {"create": "newco"})
    nav = re.search(r"<nav>(.*?)</nav>", request("GET", "/")[2]).group(1)
    assert re.findall(r">([^<]+)</a>", nav) == [
        "Inventory", "Installation", "English", "Français"]


def test_creating_an_inventory_saves_it_and_opens_the_installation_page(client):
    request, root = client
    status, where, _ = request("POST", "/inventory", {"create": "newco"})
    assert (status, where) == (303, "/")
    assert (root / "newco" / run_mod.ENV_FILENAME).is_file()
    page = request("GET", "/")[2]
    assert "<h1>Install: newco</h1>" in page
    assert str(root / "newco") in page
    order = [m for m in re.findall(r'<section id="?(\w+)"?>', page)]
    assert order == [s.name for s in STEPS] + ["install", "access"]


def test_the_inventories_are_listed_one_per_line(client):
    request, _ = client
    for name in ("alpha", "beta", "gamma"):
        request("POST", "/inventory", {"create": name})
    page = request("GET", "/inventory")[2]
    rows = re.findall(r"<li><button type=submit name=open value=\"(\w+)\">", page)
    assert rows == ["alpha", "beta", "gamma"]


def test_opening_an_inventory_writes_nothing(client):
    """The `.env` is a file its client may have edited: it is written by an
    attempt to install, never by being opened."""
    request, root = client
    (root / "handmade").mkdir()
    env = root / "handmade" / ".env"
    env.write_text("# my notes\nHOST_PUBLIC_IP=198.51.100.7\n")
    request("POST", "/inventory", {"open": "handmade"})
    assert env.read_text() == "# my notes\nHOST_PUBLIC_IP=198.51.100.7\n"
    assert not (root / "handmade" / run_mod.STATE_FILENAME).exists()


def test_saving_keeps_a_key_the_template_does_not_carry(client, started):
    request, root = client
    (root / "handmade").mkdir()
    env = root / "handmade" / ".env"
    env.write_text("HOST_PUBLIC_IP=198.51.100.7\nCLIENT_OWN_KEY=kept\n")
    request("POST", "/inventory", {"open": "handmade"})
    request("POST", "/", _INSTALL)
    saved = run_mod.seed().read_existing_env(env)
    assert (saved["CLIENT_OWN_KEY"], saved["HOST_PUBLIC_IP"]) == ("kept", "198.51.100.7")


def test_a_bad_name_is_refused_on_the_page(client):
    request, root = client
    status, where, _ = request("POST", "/inventory", {"create": "Bad Name"})
    assert (status, where) == (303, "/inventory")
    assert "lower-case" in request("GET", "/inventory")[2]
    assert run_mod.seed().inventories(root) == []


def test_a_field_is_named_by_its_label_and_explains_itself_in_a_tooltip(client):
    """The label, `(optional)` and a `(?)`; the help is in the tooltip, not on
    the page."""
    request, _ = client
    request("POST", "/inventory", {"create": "newco"})
    field = next(f for s in STEPS for f in s.fields if f.optional)
    page = request("GET", "/")[2]
    head = re.search(rf'<div class=field data-key="{field.key}"[^>]*>'
                     r"<div class=head>(.*?)</div>", page, re.S).group(1)
    assert f">{escape(field.label['en'])}</label>" in head
    assert "(optional)" in head
    assert f"(?)<span class=tt role=tooltip>{escape(field.help['en'])}" in head


def test_the_switcher_shows_every_page_in_french(client):
    """`?lang=fr` remembers the choice in a cookie and shows the same page;
    the fields read their French label and help, the page its own words."""
    request, _ = client
    request("POST", "/inventory", {"create": "newco"})
    status, where, _ = request("GET", "/?lang=fr")
    assert (status, where) == (303, "/")
    assert request.cookie.startswith(f"{server.LANG_COOKIE}=fr;")
    page = request("GET", "/", headers={"Cookie": f"{server.LANG_COOKIE}=fr"})[2]
    assert "<html lang=fr>" in page
    field = next(f for s in STEPS for f in s.fields if f.key == "HOST_PUBLIC_IP")
    assert f">{escape(field.label['fr'])}</label>" in page
    assert escape(i18n.text("fr", "process.button")) in page


def test_the_browser_language_decides_until_a_choice_is_made(client):
    request, _ = client
    fr = request("GET", "/inventory",
                 headers={"Accept-Language": "fr-CA,fr;q=0.9,en;q=0.8"})[2]
    assert escape(i18n.text("fr", "inventory.create")) in fr
    en = request("GET", "/inventory", headers={
        "Accept-Language": "fr-CA", "Cookie": f"{server.LANG_COOKIE}=en"})[2]
    assert escape(i18n.text("en", "inventory.create")) in en


def test_the_target_section_ends_on_its_note(client):
    request, _ = client
    request("POST", "/inventory", {"create": "newco"})
    target = next(s for s in STEPS if s.name == "target")
    section = _section(request("GET", "/")[2], "target")
    assert target.note and section.endswith(
        f"<p class=doc>{escape(target.note['en'])}</p>")


def test_an_example_is_shown_and_never_filled_in(client):
    """A pre-filled example is an answer nobody gave."""
    request, _ = client
    request("POST", "/inventory", {"create": "newco"})
    field = next(f for s in STEPS for f in s.fields if f.example)
    page = request("GET", "/")[2]
    assert f'placeholder="{field.example}"' in page
    assert f'name="{field.key}" value=""' in page


def test_the_form_is_saved_even_when_verification_fails(client, started, monkeypatch):
    """The answers are on disk from the first attempt, and reopening the
    inventory shows them again."""
    request, root = client
    request("POST", "/inventory", {"create": "newco"})
    monkeypatch.setattr(steps_mod, "validate", lambda step, a, s, lang: [
        steps_mod.Check("reachable", False)])
    request("POST", "/", {**_REQUIRED, **_INSTALL})
    assert started == []
    saved = run_mod.seed().read_existing_env(root / "newco" / ".env")
    assert saved["ADMIN_EMAIL"] == "admin@client.test"

    request("POST", "/inventory", {"create": "other"})
    request("POST", "/inventory", {"open": "newco"})
    assert 'value="admin@client.test"' in request("GET", "/")[2]


def test_one_button_verifies_every_section_and_shows_why_it_stopped(
        client, started, monkeypatch):
    request, _ = client
    request("POST", "/inventory", {"create": "newco"})
    seen = []

    def validate(step, answers, secrets, lang):
        seen.append(step)
        return [steps_mod.Check(f"probe of {step}", step != "target")]

    monkeypatch.setattr(steps_mod, "validate", validate)
    status, where, _ = request("POST", "/", {**_REQUIRED, **_INSTALL})
    assert (status, where) == (303, "/#install")
    assert seen == [s.name for s in STEPS]
    assert started == []
    install = _section(request("GET", "/")[2], "install")
    assert install.index("probe of target") > install.index(
        "Verify configuration and start installation</button>")


def test_the_install_starts_when_every_section_passes(client, started):
    request, _ = client
    request("POST", "/inventory", {"create": "newco"})
    status, where, _ = request("POST", "/", {**_REQUIRED, **_INSTALL})
    assert (status, where) == (303, "/#install")
    assert len(started) == 1


def test_a_required_field_left_empty_stops_the_install(client, started):
    """No probe runs on a section whose required answers are missing, and the
    install does not start."""
    request, _ = client
    request("POST", "/inventory", {"create": "newco"})
    request("POST", "/", _INSTALL)
    assert started == []
    install = _section(request("GET", "/")[2], "install")
    assert "Host public IP is required" in install
    assert "Admin email is required" in install


def test_the_form_waits_while_an_install_runs_and_takes_any_other_state(
        client, started):
    """Running the install again on an installed server converges and repairs
    it, so after a failure or a success the form takes answers and the button
    again; while an install runs it takes neither."""
    request, _ = client
    request("POST", "/inventory", {"create": "newco"})
    current = server._Handler.run

    current.state = run_mod.STATE_INSTALLING
    assert "<fieldset disabled>" in request("GET", "/")[2]
    assert request("POST", "/", {**_REQUIRED, **_INSTALL})[1] == "/#install"
    assert started == []

    for state in (run_mod.STATE_FAILED, run_mod.STATE_DONE):
        current.state = state
        assert "<fieldset>" in request("GET", "/")[2]
        request("POST", "/", {**_REQUIRED, **_INSTALL})
    assert len(started) == 2


def test_the_page_asks_for_the_server_and_nothing_the_panel_holds(client):
    request, _ = client
    request("POST", "/inventory", {"create": "newco"})
    page = request("GET", "/")[2]
    asked = set(re.findall(r'<div class=field data-key="(\w+)"', page))
    held = {e["key"] for e in [*DOC["secrets"], *DOC["config"]] if e.get("panel")}
    assert asked and not (asked & held), asked & held


def test_the_access_section_names_the_forward_and_both_panels(client, started):
    request, _ = client
    request("POST", "/inventory", {"create": "newco"})
    request("POST", "/", {**_REQUIRED, **_INSTALL})
    access = _section(request("GET", "/")[2], "access")
    assert "panel@203.0.113.10" in access
    assert "http://localhost:9010" in access and "http://localhost:9000" in access
    assert "Settings tab" in access
    for name in server.KEYSET_FIELDS:
        assert escape(i18n.text("en", f"keyset.{name}")) in access


def test_suggestions_are_offered_beside_a_free_text_field():
    """The key the provider installed is usually one of the pairs already on
    this machine, and sometimes it is not: a list to pick from, and a field
    that takes any path."""
    field = steps_mod.Field(key="SSH_PRIVATE_KEY", label={"en": "SSH key file"},
                            secret=False, optional=False, options=[], default="",
                            help={"en": ""}, suggestions=["~/.ssh/id_ed25519"])
    html = server._field_html(field, "~/.ssh/catena_ed25519", "en")
    assert 'list="s-SSH_PRIVATE_KEY"' in html
    assert '<datalist id="s-SSH_PRIVATE_KEY"><option value="~/.ssh/id_ed25519">' in html
    assert 'value="~/.ssh/catena_ed25519"' in html


def _keyset_script(values: dict) -> str:
    cli = registry.ansible_module("catena_cli")
    return "\n".join((
        "print('\\x1b[1;34m== Stage: bootstrap\\x1b[0m')",
        f"print({cli.KEYSET_BEGIN!r})",
        f"print({json.dumps(values)!r})",
        f"print({cli.KEYSET_END!r})",
        "print('converge')"))


def test_the_passwords_are_read_apart_from_the_log_and_kept_off_the_console(
        tmp_path, monkeypatch, capsys):
    """The JSON the CLI frames goes to the run's keyset, which the page shows
    on its own, and not to the console window; every other line, its colour
    codes stripped, to the log."""
    values = {"admin_password": "pw", "console_recovery_password": "cpw",
              "journal_verification_key": "fss"}
    monkeypatch.setattr(server.render, "install_command",
                        lambda *a, **k: [sys.executable, "-c", _keyset_script(values)])
    current = run_mod.load(tmp_path / "clientco", run_mod.secret_keys_from(DOC))
    server.start_install(current, tmp_path).join(timeout=30)
    assert current.keyset == values
    assert list(current.log) == ["== Stage: bootstrap", "converge"]
    assert "cpw" not in capsys.readouterr().err


def test_the_gui_asks_the_cli_for_the_values(tmp_path, monkeypatch):
    seen = {}

    def command(*args, **kwargs):
        seen.update(kwargs)
        return [sys.executable, "-c", "pass"]

    monkeypatch.setattr(server.render, "install_command", command)
    current = run_mod.load(tmp_path / "clientco", run_mod.secret_keys_from(DOC))
    server.start_install(current, tmp_path).join(timeout=30)
    assert seen == {"keyset_json": True}


def test_the_install_reads_no_input(tmp_path, monkeypatch):
    """Nothing on the page can answer a question: the install's stdin is empty
    and no terminal, so a prompt takes its default instead of waiting."""
    monkeypatch.setattr(server.render, "install_command", lambda *a, **k: [
        sys.executable, "-c",
        "import sys; print(sys.stdin.isatty(), repr(sys.stdin.read()))"])
    current = run_mod.load(tmp_path / "clientco", run_mod.secret_keys_from(DOC))
    server.start_install(current, tmp_path).join(timeout=30)
    assert list(current.log) == ["False ''"]


def test_the_passwords_appear_in_the_access_section_and_the_output_scrolls(client):
    """Each value has its own place, which says it is coming until the install
    prints it. The output is a scrolling box the page polls, not a page that
    reloads."""
    request, _ = client
    request("POST", "/inventory", {"create": "newco"})
    current = server._Handler.run
    current.state = run_mod.STATE_INSTALLING
    page = request("GET", "/")[2]
    pending = i18n.text("en", "keyset.pending")
    assert f'<code id="k-admin_password">{pending}</code>' in page
    assert '<pre id=log class=log data-state="installing">' in page
    assert "http-equiv=refresh" not in page

    current.keyset = {"admin_password": "pw", "console_recovery_password": "cpw",
                      "journal_verification_key": ""}
    current.log.append("converge")
    access = _section(request("GET", "/")[2], "access")
    assert '<code id="k-admin_password">pw</code>' in access
    assert '<code id="k-console_recovery_password">cpw</code>' in access
    not_again = i18n.text("en", "keyset.not_again")
    assert f'<code id="k-journal_verification_key">{not_again}</code>' in access
    status, _, body = request("GET", "/progress")
    assert status == 200
    assert json.loads(body) == {
        "state": "installing", "log": "converge",
        "keyset": {"admin_password": "pw", "console_recovery_password": "cpw",
                   "journal_verification_key": not_again}}


def test_the_install_output_reaches_no_file(tmp_path, monkeypatch):
    """`catena-cli install` prints the passwords it shows once. The page shows
    them from memory; no file in the inventory, or anywhere the launcher
    writes, keeps them."""
    values = {"admin_password": "SHOWN-ONCE"}
    monkeypatch.setattr(server.render, "install_command",
                        lambda *a, **k: [sys.executable, "-c", _keyset_script(values)])
    current = run_mod.load(tmp_path / "clientco", run_mod.secret_keys_from(DOC))
    server.start_install(current, tmp_path).join(timeout=30)
    assert current.state == run_mod.STATE_DONE
    assert current.keyset == values
    for path in (tmp_path / "clientco").rglob("*"):
        if path.is_file():
            assert "SHOWN-ONCE" not in path.read_text(errors="replace"), path


def test_a_credential_typed_on_the_page_reaches_no_file(client, started):
    request, root = client
    request("POST", "/inventory", {"create": "newco"})
    secret = next(f for s in STEPS for f in s.fields if f.secret)
    request("POST", "/", {secret.key: "do-not-write-me", **_INSTALL})
    for path in (root / "newco").rglob("*"):
        if path.is_file():
            assert "do-not-write-me" not in path.read_text(errors="replace"), path
