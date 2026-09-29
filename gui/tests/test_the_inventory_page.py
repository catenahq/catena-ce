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
import re
import threading
import urllib.parse

import pytest

from catena_gui import registry, run as run_mod, server, steps as steps_mod

DOC = registry.load()
STEPS = steps_mod.build(DOC)


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
    server._Handler.found_options = {}
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


def _plain_field(step: str) -> steps_mod.Field:
    built = next(s for s in STEPS if s.name == step)
    return next(f for f in built.fields if not f.secret and not f.options)


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


def test_checking_one_section_saves_every_section(client):
    """One form: a check further up keeps what was typed further down."""
    request, root = client
    request("POST", "/inventory", {"create": "newco"})
    first, other = _plain_field(STEPS[0].name), _plain_field("backup")
    status, where, _ = request("POST", "/", {
        first.key: "first-value", other.key: "other-value",
        "check": STEPS[0].name})
    assert (status, where) == (303, f"/#{STEPS[0].name}")
    saved = run_mod._seed().read_existing_env(root / "newco" / ".env")
    assert (saved[first.key], saved[other.key]) == ("first-value", "other-value")

    request("POST", "/inventory", {"create": "other"})
    request("POST", "/inventory", {"open": "newco"})
    assert 'value="other-value"' in request("GET", "/")[2]


def test_a_check_shows_under_its_own_button(client, monkeypatch):
    request, _ = client
    request("POST", "/inventory", {"create": "newco"})
    monkeypatch.setattr(steps_mod, "validate", lambda step, a, s: [
        steps_mod.Check(f"probe of {step}", True)])
    request("POST", "/", {"check": "domain"})
    page = request("GET", "/")[2]
    domain = _section(page, "domain")
    assert domain.index("probe of domain") > domain.index(">Check</button>")
    assert "probe of" not in _section(page, STEPS[0].name)


def test_install_checks_every_section_and_stops_on_the_first_that_fails(
        client, monkeypatch):
    request, _ = client
    request("POST", "/inventory", {"create": "newco"})
    started = []
    monkeypatch.setattr(server, "start_install", lambda *a: started.append(a))
    seen = []

    def validate(step, answers, secrets):
        seen.append(step)
        return [steps_mod.Check("reachable", step != "access")]

    monkeypatch.setattr(steps_mod, "validate", validate)
    status, where, _ = request("POST", "/", {"install": "yes", "ack": "yes",
                                             **_REQUIRED})
    assert (status, where) == (303, "/#access")
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


def test_the_access_method_is_saved_from_the_credentials(client):
    """Not a question on the page: it follows from whether the tailnet
    section was answered, and reaches the .env the installer reads."""
    request, root = client
    request("POST", "/inventory", {"create": "newco"})
    request("POST", "/", {"check": "access"})
    saved = run_mod._seed().read_existing_env(root / "newco" / ".env")
    assert saved["ACCESS_METHOD"] == "public_ssh"
    request("POST", "/", {"tailscale_oauth_client_id": "x",
                          "tailscale_oauth_client_secret": "y", "check": "access"})
    saved = run_mod._seed().read_existing_env(root / "newco" / ".env")
    assert saved["ACCESS_METHOD"] == "tailnet"


def test_a_section_with_nothing_to_prove_has_no_check_button(client):
    request, _ = client
    request("POST", "/inventory", {"create": "newco"})
    page = request("GET", "/")[2]
    quiet = [s.name for s in STEPS if not s.validates]
    assert quiet, "every section has a check; nothing to assert"
    for name in quiet:
        assert 'name=check' not in _section(page, name), name


def test_a_check_fills_the_list_it_found(client, monkeypatch):
    """The domains a token reaches become the domain field's choices, with a
    blank to leave it unset."""
    request, _ = client
    request("POST", "/inventory", {"create": "newco"})
    monkeypatch.setattr(steps_mod, "options_after_check",
                        lambda step, a, s: {"CLOUDFLARE_ZONE": ["client.test"]}
                        if step == "domain" else {})
    request("POST", "/", {"check": "domain"})
    section = _section(request("GET", "/")[2], "domain")
    assert re.search(r'<select id="f-CLOUDFLARE_ZONE"[^>]*>'
                     r'<option value="" selected>\(none\)</option>'
                     r'<option value="client.test">', section), section


def test_the_access_page_names_the_ssh_forward(client):
    request, _ = client
    request("POST", "/inventory", {"create": "newco"})
    request("POST", "/", {"HOST_PUBLIC_IP": "203.0.113.10", "check": "target"})
    page = request("GET", "/access")[2]
    assert "Reaching the panel" in page
    assert "panel@203.0.113.10" in page


def test_the_acknowledgement_box_comes_before_its_text(client):
    request, _ = client
    request("POST", "/inventory", {"create": "newco"})
    last = _section(request("GET", "/")[2], STEPS[-1].name)
    assert re.search(r"<label class=ack><input type=checkbox name=ack value=yes>"
                     r"<span>", last)
    assert "name=install" in last


def test_a_governing_choice_comes_before_the_fields_it_governs(client):
    request, _ = client
    request("POST", "/inventory", {"create": "newco"})
    page = request("GET", "/")[2]
    for step in STEPS:
        body = _section(page, step.name)
        for field in step.fields:
            if field.governor in {f.key for f in step.fields}:
                assert (body.index(f'data-key="{field.governor}"')
                        < body.index(f'data-key="{field.key}"')), field.key


def test_the_provider_choice_hides_the_other_providers_fields(client):
    """The provider is always asked; the credentials shown are the chosen
    provider's."""
    request, _ = client
    request("POST", "/inventory", {"create": "newco"})

    def hidden(page):
        return set(re.findall(r'<div class=field data-key="(\w+)"[^>]* hidden>', page))

    tailscale = hidden(request("GET", "/")[2])
    assert "TAILNET_PROVIDER" not in tailscale
    assert "tailscale_oauth_client_id" not in tailscale
    assert "headscale_api_key" in tailscale
    request("POST", "/", {"TAILNET_PROVIDER": "headscale", "check": "access"})
    headscale = hidden(request("GET", "/")[2])
    assert "headscale_api_key" not in headscale
    assert "tailscale_oauth_client_id" in headscale


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


def test_a_credential_typed_on_the_page_reaches_no_file(client):
    request, root = client
    request("POST", "/inventory", {"create": "newco"})
    secret = next(f for s in STEPS for f in s.fields if f.secret)
    request("POST", "/", {secret.key: "do-not-write-me", "check": STEPS[0].name})
    for path in (root / "newco").rglob("*"):
        if path.is_file():
            assert "do-not-write-me" not in path.read_text(errors="replace"), path
