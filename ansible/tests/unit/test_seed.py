"""Unit tests for the Community installer's seed.py.

Covers what seed owns under 0b: it holds no vault and mints nothing, it
collects no vendor credential and refuses the settings the server holds, it
reads its question list from the knob registry, and it writes the inventory
files plus one transient adopt map (the admin override).
"""
from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path

import pytest
import yaml

ANSIBLE_DIR = Path(__file__).resolve().parents[2]
SEED_PATH = ANSIBLE_DIR / "seed.py"
if str(ANSIBLE_DIR) not in sys.path:
    sys.path.insert(0, str(ANSIBLE_DIR))

from helpers import render_knobs  # noqa: E402


@pytest.fixture(scope="module")
def seed():
    spec = importlib.util.spec_from_file_location("seed_py", SEED_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- load_input -------------------------------------------------------------
def test_load_input_flat_layout(seed, tmp_path):
    src = tmp_path / "install.yaml"
    src.write_text(
        "inventory: prod\n"
        "host_name: prod1\n"
        "host_public_ip: 203.0.113.10\n"
        "ADMIN_EMAIL: admin@client.test\n"
        "admin_password: pinned-admin-password\n"
    )
    got = seed.load_input(src)
    assert got["inventory"] == "prod"
    assert got["host"]["name"] == "prod1"
    assert got["host"]["public_ip"] == "203.0.113.10"
    assert got["env"]["ADMIN_EMAIL"] == "admin@client.test"
    assert got["vault"]["admin_password"] == "pinned-admin-password"


def test_load_input_nested_layout(seed, tmp_path):
    src = tmp_path / "install.yaml"
    src.write_text(
        "inventory: prod\n"
        "host:\n"
        "  name: prod1\n"
        "env:\n"
        "  ADMIN_EMAIL: admin@client.test\n"
        "vault:\n"
        "  admin_password: pinned-admin-password\n"
    )
    got = seed.load_input(src)
    assert got["host"] == {"name": "prod1"}
    assert got["env"]["ADMIN_EMAIL"] == "admin@client.test"
    assert got["vault"]["admin_password"] == "pinned-admin-password"


def test_load_input_none_returns_empty(seed):
    assert seed.load_input(None) == {
        "inventory": None, "host": {}, "env": {}, "vault": {},
    }


def test_load_input_drops_legacy_vault_password(seed, tmp_path):
    src = tmp_path / "install.yaml"
    src.write_text(
        "inventory: prod\n"
        "vault_password: legacy\n"
        "admin_password: pinned-admin-password\n"
    )
    got = seed.load_input(src)
    assert "vault_password" not in got
    assert "vault_password" not in got["vault"]
    assert got["vault"]["admin_password"] == "pinned-admin-password"


def test_load_input_missing_path_dies(seed, tmp_path):
    with pytest.raises(SystemExit):
        seed.load_input(tmp_path / "no-such-file.yaml")


def test_no_client_age_pubkey_field(seed):
    """Community is single-recipient: the operator+client dual-recipient
    install.yaml field is gone."""
    assert "client_age_pubkey" not in seed.load_input(None)


# --- what the installer does not take ---------------------------------------
def _inp(env=None, vault=None):
    return {"inventory": "prod", "host": {}, "env": env or {}, "vault": vault or {}}


def test_the_server_s_settings_are_not_install_inputs(seed):
    """The domain, the tailnet, the backups and the subdomains are the
    server's, entered in the panel. So is every vendor credential."""
    refused = seed.not_install_inputs(_inp(
        env={"CLOUDFLARE_ZONE": "client.test", "TAILNET_PROVIDER": "tailscale",
             "TAILSCALE_TAGS": "tag:vps", "BACKUP_RESTIC_REPO": "s3:x/y",
             "PORTAINER_SUBDOMAIN": "admin"},
        vault={"cloudflare_api_token": "t", "tailscale_oauth_client_id": "i",
               "tailscale_oauth_client_secret": "s", "headscale_api_key": "h",
               "backup_s3_access_key": "a", "backup_s3_secret_key": "b",
               "backup_restic_password": "r"}))
    assert refused == sorted([
        "CLOUDFLARE_ZONE", "TAILNET_PROVIDER", "TAILSCALE_TAGS",
        "BACKUP_RESTIC_REPO", "PORTAINER_SUBDOMAIN", "cloudflare_api_token",
        "tailscale_oauth_client_id", "tailscale_oauth_client_secret",
        "headscale_api_key", "backup_s3_access_key", "backup_s3_secret_key",
        "backup_restic_password"])


def test_the_installer_s_own_keys_and_the_admin_override_are_taken(seed):
    env = {key: "x" for key, _ in seed.ENV_KEYS}
    assert seed.not_install_inputs(_inp(
        env=env, vault={"admin_password": "x" * 20})) == []


def test_a_key_nothing_declares_is_not_refused(seed):
    """Not this check's concern: a key neither the registry nor the store
    declares reaches nothing either way."""
    assert seed.not_install_inputs(_inp(env={"SOMETHING_ELSE": "x"})) == []


def test_an_install_naming_one_stops_before_writing_anything(seed, tmp_path):
    src = tmp_path / "install.yaml"
    src.write_text("inventory: prod\nCLOUDFLARE_ZONE: client.test\n")
    inv = tmp_path / "inv"
    with pytest.raises(SystemExit):
        seed.main(["-i", str(src), "--inventory-path", str(inv), "--no-confirm",
                   "--secrets-out", str(tmp_path / "s.yml")])
    assert not inv.exists()
    assert not (tmp_path / "s.yml").exists()


def test_a_filled_env_line_the_server_holds_is_named(seed, tmp_path, capsys):
    """Nothing reads it: the store seeds only the keys the `.env` declares."""
    env = tmp_path / ".env"
    env.write_text("ADMIN_EMAIL=a@client.test\nCLOUDFLARE_ZONE=client.test\n"
                   "PORTAINER_SUBDOMAIN=\n")
    seed.warn_server_held_lines(env)
    err = capsys.readouterr().err
    assert "sets CLOUDFLARE_ZONE," in err
    assert "catena-admin > Settings" in err


def test_an_env_with_only_installer_lines_says_nothing(seed, tmp_path, capsys):
    env = tmp_path / ".env"
    env.write_text("ADMIN_EMAIL=a@client.test\nCLIENT_OWN_KEY=kept\n")
    seed.warn_server_held_lines(env)
    assert capsys.readouterr().err == ""


def test_the_seed_time_account_fetch_is_gone(seed):
    """The account id is not a seed input: the host engine resolves it from the
    token, so a second resolver here would be a second answer."""
    for gone in ("fetch_cloudflare_account_id", "_resolve_cloudflare_account",
                 "_install_secret_keys"):
        assert not hasattr(seed, gone), f"{gone} should be removed"


def test_vault_template_machinery_is_gone(seed):
    """No persisted laptop vault: the vault template + skip-set + emit are
    removed."""
    for gone in ("VAULT_SKIP_KEYS", "VAULT_TEMPLATE", "parse_vault_template",
                 "emit_vault", "_collect_vault_values"):
        assert not hasattr(seed, gone), f"{gone} should be removed"


# --- the question list comes from the registry ------------------------------
def test_the_prompts_are_the_registrys_env_knobs(seed):
    """Which keys seed asks about, and what each defaults to, is declared once.

    A hand-kept list here is a fourth copy of the same names, beside the
    template, the store's two maps and the panel's schema. Copies of one
    declaration drift, which is what the registry exists to stop: so the list
    is DERIVED, and the template it prompts from renders from the same source.
    """
    declared = [
        (knob["key"], knob["env"]["default"])
        for knob in render_knobs.env_knobs(render_knobs.rendered())
    ]
    assert seed.ENV_KEYS == declared


def test_the_prompt_order_is_the_template_order(seed):
    """A client answering prompts and a client editing the file walk the same
    sequence, or the two surfaces describe the install in different orders."""
    in_template = [key for key, _ in seed.parse_env_pairs(seed.ENV_TEMPLATE.read_text())]
    assert [key for key, _ in seed.ENV_KEYS] == in_template


def test_env_options_are_the_registrys_enumerations(seed):
    """Derived, so the set is whatever the registry enumerates. Held against
    the registry rather than a literal list, which would be the copy this whole
    derivation exists to remove."""
    declared = {
        knob["key"]: knob["env"]["options"]
        for knob in render_knobs.env_knobs(render_knobs.rendered())
        if "options" in knob["env"]
    }
    assert seed.ENV_OPTIONS == declared


def test_env_options_drop_managed_lifecycle_knobs(seed):
    """Auto-update + scheduled-backup tiers are Business managed-lifecycle
    features, absent from the Community template + wizard. STORAGE_MODE is not
    a Community knob either: the product owns one data prefix on the disk the
    host already has and manages no block devices."""
    for key in ("AUTO_UPDATE_MODE", "AUTO_UPDATE_REBOOT",
                "AUTO_UPDATE_PROVIDER", "BACKUP_TIER",
                "STORAGE_MODE", "STORAGE_BLOCK_DEVICE"):
        assert key not in seed.ENV_OPTIONS


def test_admin_password_min_len_only(seed):
    """Only the override floor remains; there is no auto-mint length (the admin
    password is minted on-box, not by seed)."""
    assert seed.ADMIN_PASSWORD_MIN_LEN >= 16
    assert not hasattr(seed, "ADMIN_PASSWORD_AUTO_LEN")


# --- emit_env ---------------------------------------------------------------
def test_emit_env_preserves_existing_values(seed, tmp_path):
    template = (
        "# comment\n"
        "SMTP_HOST=default.example\n"
        "SMTP_PORT=587\n"
        "ADMIN_EMAIL=placeholder\n"
    )
    target = tmp_path / ".env"
    target.write_text("# comment\nSMTP_HOST=mine.smtp.example\nSMTP_PORT=587\n")
    seed.emit_env(template, {
        "SMTP_HOST": "input-value.example",
        "SMTP_PORT": "587",
        "ADMIN_EMAIL": "admin@client.test",
    }, target)
    contents = target.read_text()
    assert "SMTP_HOST=mine.smtp.example" in contents
    assert "ADMIN_EMAIL=admin@client.test" in contents


def test_emit_env_quotes_values_with_whitespace(seed, tmp_path):
    target = tmp_path / ".env"
    seed.emit_env("NTFY_TOPIC=\n", {"NTFY_TOPIC": "topic with spaces"}, target)
    assert '"topic with spaces"' in target.read_text()


@pytest.mark.parametrize("keep_existing", [True, False])
def test_emit_env_keeps_a_key_the_template_does_not_carry(seed, tmp_path,
                                                          keep_existing):
    """The template decides the layout, not which of a client's lines
    survive: a key only the file has comes back after the template's."""
    target = tmp_path / ".env"
    target.write_text("SMTP_PORT=587\nCLIENT_OWN_KEY=kept value\n")
    seed.emit_env("SMTP_PORT=25\n", {"SMTP_PORT": "2525"}, target,
                  keep_existing=keep_existing)
    pairs = seed.read_existing_env(target)
    assert pairs["CLIENT_OWN_KEY"] == "kept value"
    assert pairs["SMTP_PORT"] == ("587" if keep_existing else "2525")


# --- read_existing_env -------------------------------------------------------
def test_read_existing_env_parses_a_hand_filled_file(seed, tmp_path):
    """A self-hoster's own inventory/<name>/.env feeds _collect_env_values as
    `provided` the same way install.yaml's env: block does -- no prompting for
    anything already answered in the file."""
    target = tmp_path / ".env"
    target.write_text(
        "# comment\n"
        "ADMIN_EMAIL=admin@client.test\n"
        "NTFY_TOPIC=\"topic with spaces\"\n"
        "\n"
    )
    assert seed.read_existing_env(target) == {
        "ADMIN_EMAIL": "admin@client.test",
        "NTFY_TOPIC": "topic with spaces",
    }


# --- emit_hosts_yml ---------------------------------------------------------
def test_emit_hosts_yml_copies_the_skeleton(seed, tmp_path):
    """hosts.yml is static -- every field reads from .env at ansible runtime
    via the dotenv lookup, so seed just copies the skeleton once."""
    target = tmp_path / "hosts.yml"
    seed.emit_hosts_yml(target)
    assert target.read_text() == seed.HOSTS_YML_SKEL.read_text()


def test_emit_hosts_yml_does_not_overwrite_existing(seed, tmp_path):
    target = tmp_path / "hosts.yml"
    target.write_text("# hand-edited, e.g. a second host\n")
    seed.emit_hosts_yml(target)
    assert target.read_text() == "# hand-edited, e.g. a second host\n"


# --- emit_hosts_yml_entry (the -i install.yaml generate path) ---------------
def test_emit_hosts_yml_entry_creates_both_groups(seed, tmp_path):
    target = tmp_path / "hosts.yml"
    seed.emit_hosts_yml_entry(target, "prod1", {
        "HOST_PUBLIC_IP": "203.0.113.10",
        "HOST_INITIAL_USER": "debian",
        "HOST_SSH_PORT": "22",
        "OPS_USER": "ops",
    })
    data = yaml.safe_load(target.read_text())
    vps = data["all"]["children"]["vps"]["hosts"]
    boot = data["all"]["children"]["bootstrap"]["hosts"]
    assert boot["prod1-bootstrap"]["ansible_host"] == "203.0.113.10"
    assert boot["prod1-bootstrap"]["bootstrap_initial_user"] == "debian"
    assert vps["prod1"]["ansible_host"] == "0.0.0.0"  # bootstrap.yml rewrites this
    assert vps["prod1"]["ansible_user"] == "ops"
    assert vps["prod1"]["public_ip"] == "203.0.113.10"


def test_emit_hosts_yml_entry_merges_into_existing(seed, tmp_path):
    """The bench adds a distinctly-named host per run/slot to the same
    inventory -- an existing entry must survive, not just the new one."""
    target = tmp_path / "hosts.yml"
    target.write_text(yaml.safe_dump({
        "all": {"children": {
            "vps": {"hosts": {"old1": {"ansible_host": "100.9.9.9",
                                       "ansible_user": "ops",
                                       "ansible_port": 22}}},
            "bootstrap": {"hosts": {}},
        }}
    }))
    seed.emit_hosts_yml_entry(target, "prod1", {"HOST_PUBLIC_IP": "203.0.113.10"})
    vps = yaml.safe_load(target.read_text())["all"]["children"]["vps"]["hosts"]
    assert "old1" in vps and "prod1" in vps


def test_emit_hosts_yml_entry_defaults_when_env_values_sparse(seed, tmp_path):
    target = tmp_path / "hosts.yml"
    seed.emit_hosts_yml_entry(target, "prod1", {})
    data = yaml.safe_load(target.read_text())
    boot = data["all"]["children"]["bootstrap"]["hosts"]["prod1-bootstrap"]
    assert boot["bootstrap_initial_user"] == "root"
    assert boot["ansible_port"] == "22"


# --- write_secrets_out (transient adopt map, no persisted vault) ------------
def test_write_secrets_out_0600_and_drops_blanks(seed, tmp_path):
    out = tmp_path / "s.yml"
    seed.write_secrets_out(out, {
        "admin_password": "x" * 20,
        "blank": "",               # blank dropped
        "placeholder": "REPLACE",  # placeholder dropped
    })
    assert yaml.safe_load(out.read_text()) == {"admin_password": "x" * 20}
    assert (out.stat().st_mode & 0o777) == 0o600


def test_write_secrets_out_empty_writes_empty_map(seed, tmp_path):
    out = tmp_path / "s.yml"
    seed.write_secrets_out(out, {})
    # An empty (but present) 0600 file: `-e @file` loads {} -- harmless.
    assert (yaml.safe_load(out.read_text()) or {}) == {}
    assert (out.stat().st_mode & 0o777) == 0o600


# --- admin-password override (optional install.yaml pin) --------------------
def test_admin_override_too_short_dies(seed):
    with pytest.raises(SystemExit):
        seed._resolve_admin_override({}, {"admin_password": "short"})


def test_admin_override_accepts_long(seed):
    values: dict = {}
    seed._resolve_admin_override(values, {"admin_password": "x" * 20})
    assert values["admin_password"] == "x" * 20


def test_admin_override_noop_when_absent(seed):
    values: dict = {}
    seed._resolve_admin_override(values, {})
    assert values == {}


def test_seed_has_no_sops_age_helpers(seed):
    """No self-recipient / age-key machinery remains: emit_self_sops_yaml and
    _resolve_self_age_key do not exist."""
    for gone in ("emit_self_sops_yaml", "_resolve_self_age_key"):
        assert not hasattr(seed, gone), f"{gone} should be removed"


# --- validate_install_structural --------------------------------------------
def _good_inp():
    return {"inventory": "prod", "host": {}, "env": {}, "vault": {}}


_ENV_KEYS = [("APT_PROXY_URL", "")]  # optional: blank default


def test_validate_structural_clean(seed):
    assert seed.validate_install_structural(_good_inp(), _ENV_KEYS) == 0


_FAIL_MARK = "\033[1;31m"  # the red x _check() prints for a failed check


def _prereq_lines(seed, capsys, env):
    seed.validate_install(
        {"inventory": "prod", "host": {}, "env": env, "vault": {}}, [])
    out = capsys.readouterr().err
    body = out.split("Local prerequisites")[1]
    return [line for line in body.splitlines() if "SSH" in line]


def test_missing_ssh_key_is_not_reported_as_a_failed_check(seed, capsys, tmp_path):
    """A missing keypair is not a problem -- ensure_ssh_key() offers to generate
    it. A line that asserts the file exists, marks that assertion false, and in
    the same breath promises to generate the file reads as both 'exists' and
    'does not exist' at once."""
    absent = tmp_path / "nope"
    lines = _prereq_lines(seed, capsys, {"SSH_PRIVATE_KEY": str(absent)})
    assert len(lines) == 2
    for line in lines:
        assert _FAIL_MARK not in line
        assert "exists" not in line
        assert "not on this machine" in line


def test_present_ssh_key_says_found(seed, capsys, tmp_path):
    """The public half is looked for beside the private key."""
    priv = tmp_path / "id"
    priv.write_text("k")
    (tmp_path / "id.pub").write_text("k")
    lines = _prereq_lines(seed, capsys, {"SSH_PRIVATE_KEY": str(priv)})
    assert len(lines) == 2
    assert all("(found)" in line for line in lines)


def test_unset_ssh_key_path_is_a_real_failure(seed, capsys):
    """Blank is the one state that IS wrong here -- and the only one that keeps
    the failure mark."""
    lines = _prereq_lines(seed, capsys, {"SSH_PRIVATE_KEY": ""})
    assert all("no path set in .env" in line and _FAIL_MARK in line for line in lines)


def test_validate_structural_blank_optional_key_is_ok(seed):
    """A key whose template default is blank takes a blank as its answer."""
    inp = _good_inp()
    inp["env"]["APT_PROXY_URL"] = ""
    assert seed.validate_install_structural(inp, _ENV_KEYS) == 0


def test_validate_structural_requires_a_key_with_an_example(seed):
    """HOST_PUBLIC_IP has no default, only an example, and an install still
    needs it: an empty default alone does not make it optional."""
    assert "HOST_PUBLIC_IP" in seed.ENV_EXAMPLED
    env_keys = [("HOST_PUBLIC_IP", "")]
    assert seed.validate_install_structural(_good_inp(), env_keys) >= 1
    inp = _good_inp()
    inp["env"]["HOST_PUBLIC_IP"] = "198.51.100.7"
    assert seed.validate_install_structural(inp, env_keys) == 0


def test_validate_structural_refuses_a_defaulted_key_answered_blank(seed):
    """A key with a real default is required, so a blank there counts as missing."""
    inp = _good_inp()
    inp["env"] = {"HOST_INITIAL_USER": ""}
    assert seed.validate_install_structural(inp, [("HOST_INITIAL_USER", "root")]) >= 1


def test_access_mode_is_gone(seed):
    """Only one install shape exists: a reintroduced helper or env option
    would mean a second one came back."""
    assert not hasattr(seed, "_access_mode")
    assert "ACCESS_MODE" not in seed.ENV_OPTIONS


# --- true on-box minting: seed mints NOTHING --------------------------------
def test_seed_mints_no_secrets(seed):
    """0b no-laptop-vault: seed mints nothing. Internal service secrets AND the
    user-held passwords are all minted ON-BOX (helpers/onbox_config.py)."""
    for gone in ("_resolve_service_secrets", "_mint_strong_password",
                 "_resolve_restic_password", "_resolve_admin_password",
                 "_print_secret_block", "_mint_oauth2_proxy_cookie_secret",
                 "_mint_hc_api_key", "_mint_url_safe"):
        assert not hasattr(seed, gone), f"{gone} should be removed"
    # The only secret handling left: the transient adopt-file writer + the
    # optional admin-override passthrough.
    assert hasattr(seed, "write_secrets_out")


def test_ipv4_endpoint_shows_the_bootstrap_target(seed):
    """The one field a stale inventory gets wrong silently. Echoed as the
    login+port pair bootstrap will actually dial."""
    assert seed._ipv4_endpoint({
        "HOST_PUBLIC_IP": "203.0.113.10",
        "HOST_INITIAL_USER": "debian",
        "HOST_SSH_PORT": "2222",
    }) == "debian@203.0.113.10:2222"
    # Blank must read as missing, not as a plausible address.
    assert "NOT SET" in seed._ipv4_endpoint({"HOST_PUBLIC_IP": ""})


def test_resolve_admin_override_still_present(seed):
    assert hasattr(seed, "_resolve_admin_override")


# --- the template's illustrative values are not answers -----------------------
#
# `REPLACE` stops a seed. `you@example.com` sailed through it: the
# non-interactive path returned the template default for any key the caller
# omitted, without the placeholder check the supplied-value path applies. A host
# seeded that way did not fail, it FINISHED, belonging to an address nobody
# owns.
def test_an_example_domain_is_not_an_answer(seed):
    assert seed._is_placeholder("example.com")
    assert seed._is_placeholder("EXAMPLE.COM")
    assert seed._is_placeholder("operator@example.com")
    assert seed._is_placeholder("REPLACE")


def test_a_real_domain_that_merely_contains_example_is_an_answer(seed):
    """The match is the reserved name itself, not the substring: refusing
    every domain with `example` in it would reject real ones."""
    assert not seed._is_placeholder("examplecorp.com")
    assert not seed._is_placeholder("client.example-hosting.com")
    assert not seed._is_placeholder("admin@examplecorp.com")


def test_a_non_interactive_seed_refuses_to_invent_an_address(seed, monkeypatch):
    """No TTY and no supplied value: there is nobody to ask, so it must die
    rather than hand back the illustration."""
    fake = io.StringIO("")
    fake.isatty = lambda: False
    monkeypatch.setattr("sys.stdin", fake)
    with pytest.raises(SystemExit):
        seed.fill({}, "ADMIN_EMAIL", "you@example.com")


def test_a_non_interactive_seed_still_takes_a_real_default(seed, monkeypatch):
    """The refusal is of PLACEHOLDERS, not of defaults. A template default that
    is a genuine answer still answers, or every field would need supplying."""
    fake = io.StringIO("")
    fake.isatty = lambda: False
    monkeypatch.setattr("sys.stdin", fake)
    assert seed.fill({}, "HOST_SSH_PORT", "22") == "22"


def test_the_structural_check_counts_an_example_domain_as_missing(seed):
    assert not seed._is_filled("example.com")
    assert seed._is_filled("client.test")
