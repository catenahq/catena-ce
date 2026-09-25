"""Unit tests for the Community installer's seed.py.

Covers what seed owns under 0b: it holds no vault and mints nothing, it asks
only for the credentials that JOIN the host to its tailnet, it reads its
question list from the knob registry, and it writes the inventory files plus
one transient adopt map.
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
        "CLOUDFLARE_ZONE: example.com\n"
        "cloudflare_api_token: tok123\n"
    )
    got = seed.load_input(src)
    assert got["inventory"] == "prod"
    assert got["host"]["name"] == "prod1"
    assert got["host"]["public_ip"] == "203.0.113.10"
    assert got["env"]["CLOUDFLARE_ZONE"] == "example.com"
    assert got["vault"]["cloudflare_api_token"] == "tok123"


def test_load_input_nested_layout(seed, tmp_path):
    src = tmp_path / "install.yaml"
    src.write_text(
        "inventory: prod\n"
        "host:\n"
        "  name: prod1\n"
        "env:\n"
        "  CLOUDFLARE_ZONE: example.com\n"
        "vault:\n"
        "  cloudflare_api_token: tok123\n"
    )
    got = seed.load_input(src)
    assert got["host"] == {"name": "prod1"}
    assert got["env"]["CLOUDFLARE_ZONE"] == "example.com"
    assert got["vault"]["cloudflare_api_token"] == "tok123"


def test_load_input_none_returns_empty(seed):
    assert seed.load_input(None) == {
        "inventory": None, "host": {}, "env": {}, "vault": {},
    }


def test_load_input_drops_legacy_vault_password(seed, tmp_path):
    src = tmp_path / "install.yaml"
    src.write_text(
        "inventory: prod\n"
        "vault_password: legacy\n"
        "cloudflare_api_token: tok123\n"
    )
    got = seed.load_input(src)
    assert "vault_password" not in got
    assert "vault_password" not in got["vault"]
    assert got["vault"]["cloudflare_api_token"] == "tok123"


def test_load_input_missing_path_dies(seed, tmp_path):
    with pytest.raises(SystemExit):
        seed.load_input(tmp_path / "no-such-file.yaml")


def test_no_client_age_pubkey_field(seed):
    """Community is single-recipient: the operator+client dual-recipient
    install.yaml field is gone."""
    assert "client_age_pubkey" not in seed.load_input(None)


# --- INSTALL_EXTERNAL_KEYS (every secret the installer may collect) ---------
def test_install_external_keys_are_the_creds_a_panel_cannot_take_first(seed):
    """0b no-laptop-vault: seed collects the tailnet join credential and the
    Cloudflare token, writes them to the transient --secrets-out file, and keeps
    nothing. Everything else is minted on-box or set post-install."""
    assert seed.INSTALL_EXTERNAL_KEYS == (
        "tailscale_oauth_client_id",
        "tailscale_oauth_client_secret",
        "headscale_api_key",
        "headscale_preauth_key",
        "cloudflare_api_token",
    )
    assert seed.TAILSCALE_OAUTH_KEYS == (
        "tailscale_oauth_client_id",
        "tailscale_oauth_client_secret",
    )


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
        "CLOUDFLARE_ZONE=placeholder\n"
    )
    target = tmp_path / ".env"
    target.write_text("# comment\nSMTP_HOST=mine.smtp.example\nSMTP_PORT=587\n")
    seed.emit_env(template, {
        "SMTP_HOST": "input-value.example",
        "SMTP_PORT": "587",
        "CLOUDFLARE_ZONE": "client.example.com",
    }, target)
    contents = target.read_text()
    assert "SMTP_HOST=mine.smtp.example" in contents
    assert "CLOUDFLARE_ZONE=client.example.com" in contents


def test_emit_env_quotes_values_with_whitespace(seed, tmp_path):
    target = tmp_path / ".env"
    seed.emit_env("NTFY_TOPIC=\n", {"NTFY_TOPIC": "topic with spaces"}, target)
    assert '"topic with spaces"' in target.read_text()


# --- read_existing_env -------------------------------------------------------
def test_read_existing_env_parses_a_hand_filled_file(seed, tmp_path):
    """A self-hoster's own inventory/<name>/.env feeds _collect_env_values as
    `provided` the same way install.yaml's env: block does -- no prompting for
    anything already answered in the file."""
    target = tmp_path / ".env"
    target.write_text(
        "# comment\n"
        "CLOUDFLARE_ZONE=client.example.com\n"
        "NTFY_TOPIC=\"topic with spaces\"\n"
        "\n"
    )
    assert seed.read_existing_env(target) == {
        "CLOUDFLARE_ZONE": "client.example.com",
        "NTFY_TOPIC": "topic with spaces",
    }


# --- emit_hosts_yml ---------------------------------------------------------
def test_emit_hosts_yml_copies_the_skeleton(seed, tmp_path):
    """hosts.yml is now static -- every field reads from .env at ansible
    runtime via the dotenv lookup, so seed just copies the skeleton once,
    same as emit_localhost_yml."""
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
        "cloudflare_api_token": "cf",
        "tailscale_oauth_client_id": "",   # blank dropped
        "admin_password": "REPLACE",       # placeholder dropped
    })
    assert yaml.safe_load(out.read_text()) == {"cloudflare_api_token": "cf"}
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


def test_absorb_provided_secrets_passes_through_full_keyset(seed):
    """A fully-specified install.yaml (bench / power user) supplies S3 + restic
    etc.; absorb copies every non-blank vault_* except admin (handled
    separately) into the adopt map."""
    values = {"cloudflare_api_token": "cf"}  # already collected
    seed._absorb_provided_secrets(values, {
        "backup_s3_access_key": "ak",
        "backup_s3_secret_key": "sk",
        "backup_restic_password": "rp",
        "admin_password": "should-be-ignored-here",
        "smtp_password": "",          # blank dropped
        "mailserver_relay_password": "REPLACE",  # placeholder dropped
        "not_a_vault_key": "x",             # ignored
    })
    assert values["backup_s3_access_key"] == "ak"
    assert values["backup_restic_password"] == "rp"
    assert "admin_password" not in values
    assert "smtp_password" not in values
    assert "mailserver_relay_password" not in values
    assert "not_a_vault_key" not in values


def test_seed_has_no_sops_age_helpers(seed):
    """No self-recipient / age-key machinery remains: emit_self_sops_yaml and
    _resolve_self_age_key do not exist."""
    for gone in ("emit_self_sops_yaml", "_resolve_self_age_key"):
        assert not hasattr(seed, gone), f"{gone} should be removed"


# --- validate_install_structural --------------------------------------------
def _good_inp():
    return {
        "inventory": "prod",
        # host.initial_password is the only host.* field left -- public IP,
        # initial SSH user and host name all live in .env / hosts.yml now.
        "host": {},
        # Backup repo is configured post-install in catena-admin, so a blank
        # env is a valid install.
        "env": {},
        "vault": {
            "tailscale_oauth_client_id": "x",
            "tailscale_oauth_client_secret": "y",
        },
    }


_ENV_KEYS = [("BACKUP_RESTIC_REPO", "")]  # optional now (default blank)
_VAULT_KEYS = list(("tailscale_oauth_client_id",
                    "tailscale_oauth_client_secret"))


def test_validate_structural_clean(seed):
    assert seed.validate_install_structural(_good_inp(), _ENV_KEYS, _VAULT_KEYS) == 0


_FAIL_MARK = "\033[1;31m"  # the red x _check() prints for a failed check


def _prereq_lines(seed, capsys, env):
    seed.validate_install(
        {"inventory": "prod", "host": {}, "env": env, "vault": {}}, [], [])
    out = capsys.readouterr().err
    body = out.split("Local prerequisites")[1].split("Credentials")[0]
    return [line for line in body.splitlines() if "SSH" in line]


def test_missing_ssh_key_is_not_reported_as_a_failed_check(seed, capsys, tmp_path):
    """A missing keypair is not a problem -- ensure_ssh_key() offers to generate
    it. A line that asserts the file exists, marks that assertion false, and in
    the same breath promises to generate the file reads as both 'exists' and
    'does not exist' at once."""
    absent = tmp_path / "nope"
    lines = _prereq_lines(seed, capsys, {
        "SSH_PRIVATE_KEY": str(absent), "SSH_PUBLIC_KEY_FILE": str(absent) + ".pub"})
    assert len(lines) == 2
    for line in lines:
        assert _FAIL_MARK not in line
        assert "exists" not in line
        assert "not on this machine" in line


def test_present_ssh_key_says_found(seed, capsys, tmp_path):
    priv = tmp_path / "id"
    pub = tmp_path / "id.pub"
    priv.write_text("k")
    pub.write_text("k")
    lines = _prereq_lines(seed, capsys, {
        "SSH_PRIVATE_KEY": str(priv), "SSH_PUBLIC_KEY_FILE": str(pub)})
    assert all("(found)" in line for line in lines)


def test_unset_ssh_key_path_is_a_real_failure(seed, capsys):
    """Blank is the one state that IS wrong here -- and the only one that keeps
    the failure mark."""
    lines = _prereq_lines(seed, capsys, {"SSH_PRIVATE_KEY": "", "SSH_PUBLIC_KEY_FILE": ""})
    assert all("no path set in .env" in line and _FAIL_MARK in line for line in lines)


def test_validate_structural_missing_required_vault(seed):
    inp = _good_inp()
    del inp["vault"]["tailscale_oauth_client_id"]
    assert seed.validate_install_structural(inp, _ENV_KEYS, _VAULT_KEYS) >= 1


def test_validate_structural_blank_restic_repo_is_ok(seed):
    """Backup creds are deferred to catena-admin: a blank repo at seed time is
    NOT a problem."""
    inp = _good_inp()
    inp["env"]["BACKUP_RESTIC_REPO"] = ""
    assert seed.validate_install_structural(inp, _ENV_KEYS, _VAULT_KEYS) == 0


# --- Cloudflare zone ---------------------------------------------------------
def test_validate_structural_requires_cf_zone(seed):
    """A blank zone is a problem on every host: every hostname derives from
    the zone and every host serves them."""
    inp = _good_inp()
    inp["env"] = {"CLOUDFLARE_ZONE": ""}
    env_keys = [("CLOUDFLARE_ZONE", "example.com")]
    assert seed.validate_install_structural(inp, env_keys, _VAULT_KEYS) >= 1


def test_access_mode_is_gone(seed):
    """Only one install shape exists: a reintroduced helper or env option
    would mean a second one came back."""
    assert not hasattr(seed, "_access_mode")
    assert "ACCESS_MODE" not in seed.ENV_OPTIONS


def test_collect_install_secrets_is_the_tailnet_credential_only(seed):
    """The two are collected apart because they fail apart: the tailnet one has
    no later surface to arrive through, the Cloudflare one does."""
    got = seed._collect_install_secrets({
        "tailscale_oauth_client_id": "x",
        "tailscale_oauth_client_secret": "y",
        "cloudflare_api_token": "cf-collected-by-its-own-step",
    }, {"TAILNET_CONTROL_URL": ""})
    assert got == {"tailscale_oauth_client_id": "x",
                   "tailscale_oauth_client_secret": "y"}


# --- the Cloudflare token: eager when there is a domain, deferred otherwise ---
def test_a_domain_at_install_asks_for_the_token_that_publishes_it(seed):
    """The inversion this program is built on. With both, the converge brings
    the tunnel up in the same run and the install ends reachable."""
    got = seed._collect_cloudflare_token(
        {"cloudflare_api_token": "cf-tok"}, {"CLOUDFLARE_ZONE": "client.test"})
    assert got == {"cloudflare_api_token": "cf-tok"}


def test_no_domain_asks_nothing(seed, monkeypatch):
    """A server is installed before its domain is decided. Asking for a
    credential that proves a domain nobody has named is a question with no right
    answer, so on a TTY it must consume no input."""
    _fake_tty_stdin(monkeypatch, "SHOULD_NOT_BE_READ\n")
    assert seed._collect_cloudflare_token({}, {"CLOUDFLARE_ZONE": ""}) == {}
    assert seed.sys.stdin.read() == "SHOULD_NOT_BE_READ\n"


def test_a_blank_token_beside_a_domain_still_installs(seed, monkeypatch):
    """Blank is an answer: the tunnel engine reads the store, skips, and the
    client publishes from catena-admin > Settings afterwards."""
    fake = io.StringIO("")
    fake.isatty = lambda: False
    monkeypatch.setattr("sys.stdin", fake)
    assert seed._collect_cloudflare_token({}, {"CLOUDFLARE_ZONE": "client.test"}) == {}


def _cf_responses(seed, monkeypatch, verify, zones):
    """Stub the two Cloudflare calls the probe makes, in the order it makes
    them: token verify, then the zone lookup."""
    calls: list[str] = []

    def fake(url, *, headers=None, data=None, timeout=10.0):
        calls.append(url)
        return verify if "tokens/verify" in url else zones

    monkeypatch.setattr(seed, "_http_json", fake)
    return calls


_CF_LIVE = (200, {"result": {"status": "active"}})


def test_an_active_zone_and_a_live_token_are_clean(seed, monkeypatch):
    _cf_responses(seed, monkeypatch, _CF_LIVE, (200, {"result": [
        {"id": "z1", "status": "active", "name_servers": ["a.ns", "b.ns"]}]}))
    assert seed._probe_cloudflare("tok", "client.test") == 0


def test_a_pending_zone_blocks_the_install(seed, monkeypatch, capsys):
    """Records created through the API on a pending zone resolve NOWHERE, so
    the converge publishes a wildcard nobody can follow and then waits on
    auth.<zone> forever. The remedy has to be on screen, not three roles later.
    """
    _cf_responses(seed, monkeypatch, _CF_LIVE, (200, {"result": [
        {"id": "z1", "status": "pending",
         "name_servers": ["gail.ns.cloudflare.com", "hugh.ns.cloudflare.com"]}]}))
    assert seed._probe_cloudflare("tok", "client.test") == 1
    err = capsys.readouterr().err
    assert "pending" in err
    assert "gail.ns.cloudflare.com" in err


def test_a_dead_token_blocks_before_the_zone_is_asked_about(seed, monkeypatch):
    """An expired or revoked token verifies as inactive. Stopping here means the
    second call is never made with a credential already known to be dead."""
    calls = _cf_responses(
        seed, monkeypatch, (200, {"result": {"status": "expired"}}), (200, {}))
    assert seed._probe_cloudflare("tok", "client.test") == 1
    assert all("tokens/verify" in c for c in calls)


def test_a_token_scoped_to_another_domain_blocks(seed, monkeypatch, capsys):
    """The shape a copy-paste from another account takes: the token is valid
    and grants nothing on the domain this host is about to serve."""
    _cf_responses(seed, monkeypatch, _CF_LIVE, (200, {"result": []}))
    assert seed._probe_cloudflare("tok", "client.test") == 1
    assert "Zone Resources" in capsys.readouterr().err


def test_no_token_is_not_a_problem(seed, monkeypatch, capsys):
    """The deferred install, which is a product case rather than a degraded
    one. No call is made at all."""
    calls = _cf_responses(seed, monkeypatch, _CF_LIVE, (200, {"result": []}))
    assert seed._probe_cloudflare("", "client.test") == 0
    assert seed._probe_cloudflare("tok", "") == 0
    assert calls == []
    assert "deferred" in capsys.readouterr().err


def test_collect_install_secrets_asks_for_the_headscale_key(seed):
    """Headscale has no OAuth API, so the Tailscale pair is not asked for --
    but its own join credential IS. bootstrap/roles/tailscale asserts on one of the two
    and fails the BOOTSTRAP without it, and deferring it to catena-admin is
    circular: the panel is published on the tailnet the credential joins."""
    got = seed._collect_install_secrets(
        {"headscale_api_key": "hs-api"},
        {"TAILNET_CONTROL_URL": "https://headscale.example.net"},
    )
    assert got == {"headscale_api_key": "hs-api"}
    assert "tailscale_oauth_client_id" not in got


def test_headscale_static_key_is_the_fallback(seed):
    """A Headscale whose API is not reachable from here still installs, from a
    key made by hand. Only asked for when the api_key answer was blank."""
    got = seed._collect_install_secrets(
        {"headscale_preauth_key": "hs-static"},
        {"TAILNET_CONTROL_URL": "https://headscale.example.net"},
    )
    assert got == {"headscale_preauth_key": "hs-static"}


@pytest.fixture
def on_tailnet(monkeypatch):
    """validate_install also gates on THIS machine being on the tailnet, which
    depends on the box the tests run on. Stub it so the headscale-credential
    checks below measure only themselves."""
    from helpers import tailnet_check

    monkeypatch.setattr(tailnet_check, "check",
                        lambda **kw: tailnet_check.Result(True, ["stubbed"]))


def _headscale_inp(vault):
    return {
        "inventory": "prod",
        "host": {},
        "env": {"TAILNET_CONTROL_URL": "https://hs.example.net"},
        "vault": vault,
    }


def test_headscale_install_without_a_join_credential_is_refused(seed, on_tailnet):
    """The chicken-and-egg, caught in seed where nothing has been touched yet
    rather than by bootstrap/roles/tailscale's assert partway through bootstrap."""
    assert seed.validate_install(_headscale_inp({}), [], []) >= 1


def test_headscale_install_with_either_credential_passes(seed, on_tailnet):
    for key in ("headscale_api_key", "headscale_preauth_key"):
        assert seed.validate_install(_headscale_inp({key: "x"}), [], []) == 0, key


def test_a_public_ssh_install_asks_for_no_tailnet_credential(seed):
    """ADR 0012: a host reached on port 22 joins no tailnet, so it needs no
    Tailscale account and no Headscale key."""
    assert seed._collect_install_secrets(
        {}, {"ACCESS_METHOD": "public_ssh"}) == {}
    assert seed._collect_install_secrets(
        {}, {"ACCESS_METHOD": "public_ssh",
             "TAILNET_CONTROL_URL": "https://hs.example.net"}) == {}


def test_a_public_ssh_install_validates_without_one(seed, monkeypatch):
    """No credential probe and no controller-on-the-tailnet check: neither
    has anything to do with a host that joins no tailnet."""
    from helpers import tailnet_check

    def _no_tailnet_check(**_kw):
        raise AssertionError("a public_ssh install must not check the tailnet")
    monkeypatch.setattr(tailnet_check, "check", _no_tailnet_check)
    inp = {"inventory": "prod", "host": {},
           "env": {"ACCESS_METHOD": "public_ssh"}, "vault": {}}
    assert seed.validate_install(inp, [], []) == 0


def test_blank_access_method_still_joins_the_tailnet(seed):
    assert seed._joins_tailnet({}) is True
    assert seed._joins_tailnet({"ACCESS_METHOD": ""}) is True
    assert seed._joins_tailnet({"ACCESS_METHOD": "tailnet"}) is True
    assert seed._joins_tailnet({"ACCESS_METHOD": "public_ssh"}) is False


def test_oauth_tag_follows_the_inventory_tags(seed):
    """The printed setup steps must name the tag the OAuth client will
    actually be scoped to -- the FIRST of TAILSCALE_TAGS, the same entry
    preflight mints its probe key with."""
    assert seed._oauth_tag({"TAILSCALE_TAGS": "tag:vps-test,tag:other"}) == "tag:vps-test"
    assert seed._oauth_tag({"TAILSCALE_TAGS": ""}) == "tag:vps"
    assert seed._oauth_tag({}) == "tag:vps"


# --- true on-box minting: seed mints NOTHING --------------------------------
def test_seed_mints_no_secrets(seed):
    """0b no-laptop-vault: seed mints nothing. Internal service secrets AND the
    user-held admin/restic DR keyset are all minted ON-BOX
    (helpers/onbox_config.py)."""
    for gone in ("_resolve_service_secrets", "_mint_strong_password",
                 "_resolve_restic_password", "_resolve_admin_password",
                 "_print_secret_block", "_mint_oauth2_proxy_cookie_secret",
                 "_mint_hc_api_key", "_mint_url_safe"):
        assert not hasattr(seed, gone), f"{gone} should be removed"
    # The only secret handling left: the transient adopt-file writer + the
    # optional admin-override passthrough.
    assert hasattr(seed, "write_secrets_out")


# --- tailnet backend: deduced, never asked -----------------------------------
def _fake_tty_stdin(monkeypatch, text):
    """A StringIO that reports isatty()=True, installed as sys.stdin. The
    isatty patch must land on the StringIO instance itself -- patching the
    real stdin's isatty and then replacing sys.stdin discards the patch."""
    fake = io.StringIO(text)
    fake.isatty = lambda: True
    monkeypatch.setattr("sys.stdin", fake)


def _answered(env_keys):
    """The template's defaults as a FILLED-IN inventory would carry them.

    The template's two illustrative values are not answers -- seed refuses
    them, on a TTY and off one, because a host that accepts `example.com`
    converges serving a domain nobody owns. A fixture that handed them back
    verbatim was answering every field except the two that identify the
    server, and it blocked on the prompt that refusal produces.
    """
    provided = dict(env_keys)
    provided["CLOUDFLARE_ZONE"] = "client.test"
    provided["ADMIN_EMAIL"] = "admin@client.test"
    return provided


def test_no_control_server_question_is_asked(seed):
    """The inventory declares the backend by whether the Headscale fields are
    filled. A separate question could disagree with the file it asks about, so
    there is none to ask."""
    assert not hasattr(seed, "_collect_control_server")


def test_blank_headscale_fields_are_an_answer_not_a_prompt(seed, monkeypatch):
    """A .env left blank on both Headscale fields means Tailscale SaaS. On a
    TTY that must consume no input: reading here would mean the blank was
    treated as unanswered."""
    _fake_tty_stdin(monkeypatch, "SHOULD_NOT_BE_READ\n")
    env_keys = seed.ENV_KEYS
    provided = _answered(env_keys)
    provided["TAILNET_CONTROL_URL"] = ""
    provided["HEADSCALE_USER"] = ""
    got = seed._collect_env_values(env_keys, provided)
    assert got["TAILNET_CONTROL_URL"] == ""
    assert got["HEADSCALE_USER"] == ""
    assert not seed._uses_headscale(got)
    assert seed.sys.stdin.read() == "SHOULD_NOT_BE_READ\n"


def test_filled_headscale_fields_select_headscale(seed, monkeypatch):
    _fake_tty_stdin(monkeypatch, "SHOULD_NOT_BE_READ\n")
    env_keys = seed.ENV_KEYS
    provided = _answered(env_keys)
    provided["TAILNET_CONTROL_URL"] = "https://hs.example.net"
    provided["HEADSCALE_USER"] = "alice"
    got = seed._collect_env_values(env_keys, provided)
    assert seed._uses_headscale(got)
    assert got["HEADSCALE_USER"] == "alice"
    assert seed.sys.stdin.read() == "SHOULD_NOT_BE_READ\n"


def test_tailnet_backend_names_what_was_deduced(seed):
    """Deduced, so it gets echoed: the user never confirmed it at a prompt."""
    assert "Tailscale SaaS" in seed._tailnet_backend({"TAILNET_CONTROL_URL": ""})
    line = seed._tailnet_backend(
        {"TAILNET_CONTROL_URL": "https://hs.example.net", "HEADSCALE_USER": "alice"})
    assert "Headscale" in line and "https://hs.example.net" in line and "alice" in line


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
# `REPLACE` stops a seed. `example.com` sailed through it: the non-interactive
# path returned the template default for any key the caller omitted, without
# the placeholder check the supplied-value path applies. A host seeded that way
# did not fail, it FINISHED -- converging every <sub>.<zone> hostname against a
# domain nobody owns, and telling the licence it served one.
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


def test_a_non_interactive_seed_refuses_to_invent_a_domain(seed, monkeypatch):
    """No TTY and no supplied value: there is nobody to ask, so it must die
    rather than hand back the illustration."""
    fake = io.StringIO("")
    fake.isatty = lambda: False
    monkeypatch.setattr("sys.stdin", fake)
    with pytest.raises(SystemExit):
        seed.fill({}, "CLOUDFLARE_ZONE", "example.com")


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
