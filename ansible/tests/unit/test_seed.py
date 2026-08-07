"""Unit tests for the Community installer's seed.py.

Covers the Community decomposition: the plaintext (post-SOPS, 0b) vault
emit, the trimmed VAULT_SKIP_KEYS / ENV_OPTIONS (no managed-lifecycle
knobs), the CE-only service-secret minting, and the file-emit helpers."""
from __future__ import annotations

import importlib.util
import io
from pathlib import Path

import pytest
import yaml

ANSIBLE_DIR = Path(__file__).resolve().parents[2]
SEED_PATH = ANSIBLE_DIR / "seed.py"


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


# --- INSTALL_EXTERNAL_KEYS (the only secrets prompted at install) -----------
def test_install_external_keys_are_the_tailscale_creds_only(seed):
    """0b no-laptop-vault + CF-token-Settings-only: seed prompts only for the
    Tailscale OAuth creds (needed to join the tailnet) and writes them to the
    transient --secrets-out file. The Cloudflare API token is NOT here -- it is
    entered in catena-admin > Settings. Everything else is minted on-box."""
    assert seed.INSTALL_EXTERNAL_KEYS == (
        "tailscale_oauth_client_id",
        "tailscale_oauth_client_secret",
    )


def test_cloudflare_token_is_not_an_install_input(seed):
    """The CF token is never prompted / required at install (Settings-only), and
    the seed-time auto-fetch machinery that needed it is gone."""
    assert "cloudflare_api_token" not in seed.INSTALL_EXTERNAL_KEYS
    for gone in ("fetch_cloudflare_account_id", "_resolve_cloudflare_account",
                 "_install_secret_keys"):
        assert not hasattr(seed, gone), f"{gone} should be removed"


def test_vault_template_machinery_is_gone(seed):
    """No persisted laptop vault: the vault template + skip-set + emit are
    removed."""
    for gone in ("VAULT_SKIP_KEYS", "VAULT_TEMPLATE", "parse_vault_template",
                 "emit_vault", "_collect_vault_values"):
        assert not hasattr(seed, gone), f"{gone} should be removed"


# --- ENV_OPTIONS (no managed-lifecycle knobs) -------------------------------
def test_env_options_keep_ce_enums(seed):
    assert seed.ENV_OPTIONS.get("CATENA_DEFAULT_LANGUAGE") == ["en", "fr"]
    assert seed.ENV_OPTIONS.get("STORAGE_MODE") == ["built_in", "attached"]
    assert seed.ENV_OPTIONS.get("NEXTCLOUD_VERSIONS_RETENTION") == [
        "auto, 7", "auto, 14", "auto, 30",
    ]


def test_env_options_drop_managed_lifecycle_knobs(seed):
    """Auto-update + scheduled-backup tiers are Business managed-lifecycle
    features, absent from the Community template + wizard."""
    for key in ("AUTO_UPDATE_MODE", "AUTO_UPDATE_REBOOT",
                "AUTO_UPDATE_PROVIDER", "BACKUP_TIER"):
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


# --- emit_hosts_yml ---------------------------------------------------------
def test_emit_hosts_yml_creates_both_groups(seed, tmp_path):
    target = tmp_path / "hosts.yml"
    seed.emit_hosts_yml(target, "prod1", "203.0.113.10", "100.1.2.3", "debian")
    data = yaml.safe_load(target.read_text())
    vps = data["all"]["children"]["vps"]["hosts"]
    boot = data["all"]["children"]["bootstrap"]["hosts"]
    assert vps["prod1"]["ansible_host"] == "100.1.2.3"
    assert boot["prod1-bootstrap"]["ansible_host"] == "203.0.113.10"
    assert vps["prod1"]["ansible_user"] == "ops"
    # bootstrap_initial_user is pinned as an inventory var so the Phase 1/2
    # bootstrap plays (which connect as this user) see it under --no-confirm,
    # where the play-scoped vars_prompt does not reach them.
    assert boot["prod1-bootstrap"]["bootstrap_initial_user"] == "debian"


def test_emit_hosts_yml_merges_into_existing(seed, tmp_path):
    target = tmp_path / "hosts.yml"
    target.write_text(yaml.safe_dump({
        "all": {"children": {
            "vps": {"hosts": {"old1": {"ansible_host": "100.9.9.9",
                                       "ansible_user": "ops",
                                       "ansible_port": 22}}},
            "bootstrap": {"hosts": {}},
        }}
    }))
    seed.emit_hosts_yml(target, "prod1", "203.0.113.10", "100.1.2.3", "root")
    vps = yaml.safe_load(target.read_text())["all"]["children"]["vps"]["hosts"]
    assert "old1" in vps and "prod1" in vps


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
        "nextcloud_s3_access_key": "REPLACE",  # placeholder dropped
        "not_a_vault_key": "x",             # ignored
    })
    assert values["backup_s3_access_key"] == "ak"
    assert values["backup_restic_password"] == "rp"
    assert "admin_password" not in values
    assert "smtp_password" not in values
    assert "nextcloud_s3_access_key" not in values
    assert "not_a_vault_key" not in values


def test_seed_has_no_sops_age_helpers(seed):
    """0b dropped SOPS+age: the self-recipient / age-key machinery is gone."""
    for gone in ("emit_self_sops_yaml", "_resolve_self_age_key"):
        assert not hasattr(seed, gone), f"{gone} should be removed"


# --- validate_install_structural --------------------------------------------
def _good_inp():
    return {
        "inventory": "prod",
        "host": {"name": "prod1", "public_ip": "203.0.113.10", "initial_user": "root"},
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


def test_validate_structural_rejects_client_placeholder(seed):
    """A MALFORMED repo (unreplaced <client> sentinel) still fails, even though
    a blank one is allowed."""
    inp = _good_inp()
    inp["env"]["BACKUP_RESTIC_REPO"] = "s3:s3.example.net/<client>-restic"
    assert seed.validate_install_structural(inp, _ENV_KEYS, _VAULT_KEYS) >= 1


# --- Cloudflare zone / account id -------------------------------------------
def test_validate_structural_requires_cf_zone(seed):
    """A blank zone is a problem on every host. It used to be legitimate under
    ACCESS_MODE=tailnet, which is gone: hostnames derive from the zone and
    every host now serves them."""
    inp = _good_inp()
    inp["env"] = {"CLOUDFLARE_ZONE": ""}
    env_keys = [("CLOUDFLARE_ZONE", "example.com")]
    assert seed.validate_install_structural(inp, env_keys, _VAULT_KEYS) >= 1


def test_validate_structural_allows_blank_account_id(seed):
    """CLOUDFLARE_ACCOUNT_ID is resolved on-box from the token, so a blank at
    seed time is fine (the zone is still required)."""
    inp = _good_inp()
    inp["env"] = {"CLOUDFLARE_ZONE": "example.com",
                  "CLOUDFLARE_ACCOUNT_ID": ""}
    env_keys = [("CLOUDFLARE_ZONE", "example.com"),
                ("CLOUDFLARE_ACCOUNT_ID", "REPLACE")]
    assert seed.validate_install_structural(inp, env_keys, _VAULT_KEYS) == 0


def test_access_mode_is_gone(seed):
    """The mode did not serve apps over the tailnet, it skipped the monitoring
    plane and the SSO edge entirely. Deleted 2026-07-29; a reintroduced helper
    or env option means the second install shape came back."""
    assert not hasattr(seed, "_access_mode")
    assert "ACCESS_MODE" not in seed.ENV_OPTIONS


def test_collect_install_secrets_never_collects_cf_token(seed):
    """Even when a fully-specified install.yaml carries the CF token, the
    install-secret prompt loop only iterates the Tailscale creds -- the CF token
    is Settings-only, never collected here."""
    got = seed._collect_install_secrets({
        "tailscale_oauth_client_id": "x",
        "tailscale_oauth_client_secret": "y",
        "cloudflare_api_token": "cf-should-not-be-collected",
    })
    assert got == {"tailscale_oauth_client_id": "x",
                   "tailscale_oauth_client_secret": "y"}
    assert "cloudflare_api_token" not in got


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


# --- _collect_control_server -------------------------------------------------
_CONTROL_DEFAULTS = {"TAILNET_CONTROL_URL": "", "HEADSCALE_USER": ""}


def _fake_tty_stdin(monkeypatch, text):
    """A StringIO that reports isatty()=True, installed as sys.stdin. The
    isatty patch must land on the StringIO instance itself -- patching the
    real stdin's isatty and then replacing sys.stdin discards the patch."""
    fake = io.StringIO(text)
    fake.isatty = lambda: True
    monkeypatch.setattr("sys.stdin", fake)


def test_control_server_tailscale_choice_skips_headscale_fields(seed, monkeypatch):
    """Choosing tailscale (the default) answers both keys blank without
    prompting for a Headscale URL/user that would go unused."""
    _fake_tty_stdin(monkeypatch, "tailscale\n")
    url, user = seed._collect_control_server({}, _CONTROL_DEFAULTS)
    assert (url, user) == ("", "")


def test_control_server_headscale_choice_prompts_both_fields(seed, monkeypatch):
    _fake_tty_stdin(monkeypatch, "headscale\nhttps://hs.example.net\nalice\n")
    url, user = seed._collect_control_server({}, _CONTROL_DEFAULTS)
    assert (url, user) == ("https://hs.example.net", "alice")


def test_control_server_install_yaml_answer_skips_the_choice_prompt(seed, monkeypatch):
    """install.yaml already carrying either field means the choice was already
    made -- no interactive prompt, even on a TTY."""
    _fake_tty_stdin(monkeypatch, "SHOULD_NOT_BE_READ\n")
    provided = {"TAILNET_CONTROL_URL": "https://hs2.example.net", "HEADSCALE_USER": "bob"}
    url, user = seed._collect_control_server(provided, _CONTROL_DEFAULTS)
    assert (url, user) == ("https://hs2.example.net", "bob")


def test_control_server_non_interactive_falls_back_to_defaults(seed, monkeypatch):
    """No TTY and nothing provided (CI / bench) must not hang on input()."""
    monkeypatch.setattr(seed.sys.stdin, "isatty", lambda: False)
    url, user = seed._collect_control_server({}, _CONTROL_DEFAULTS)
    assert (url, user) == ("", "")
    assert hasattr(seed, "_resolve_admin_override")
