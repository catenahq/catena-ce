"""Unit tests for the Community installer's seed.py.

Covers the Community decomposition: single-recipient SOPS, the trimmed
VAULT_SKIP_KEYS / ENV_OPTIONS (no managed-lifecycle knobs), the
CE-only service-secret minting, and the file-emit helpers."""
from __future__ import annotations

import importlib.util
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
        "vault_cloudflare_api_token: tok123\n"
    )
    got = seed.load_input(src)
    assert got["inventory"] == "prod"
    assert got["host"]["name"] == "prod1"
    assert got["host"]["public_ip"] == "203.0.113.10"
    assert got["env"]["CLOUDFLARE_ZONE"] == "example.com"
    assert got["vault"]["vault_cloudflare_api_token"] == "tok123"


def test_load_input_nested_layout(seed, tmp_path):
    src = tmp_path / "install.yaml"
    src.write_text(
        "inventory: prod\n"
        "host:\n"
        "  name: prod1\n"
        "env:\n"
        "  CLOUDFLARE_ZONE: example.com\n"
        "vault:\n"
        "  vault_cloudflare_api_token: tok123\n"
    )
    got = seed.load_input(src)
    assert got["host"] == {"name": "prod1"}
    assert got["env"]["CLOUDFLARE_ZONE"] == "example.com"
    assert got["vault"]["vault_cloudflare_api_token"] == "tok123"


def test_load_input_none_returns_empty(seed):
    assert seed.load_input(None) == {
        "inventory": None, "host": {}, "env": {}, "vault": {},
    }


def test_load_input_drops_legacy_vault_password(seed, tmp_path):
    src = tmp_path / "install.yaml"
    src.write_text(
        "inventory: prod\n"
        "vault_password: legacy\n"
        "vault_cloudflare_api_token: tok123\n"
    )
    got = seed.load_input(src)
    assert "vault_password" not in got
    assert "vault_password" not in got["vault"]
    assert got["vault"]["vault_cloudflare_api_token"] == "tok123"


def test_load_input_missing_path_dies(seed, tmp_path):
    with pytest.raises(SystemExit):
        seed.load_input(tmp_path / "no-such-file.yaml")


def test_no_client_age_pubkey_field(seed):
    """Community is single-recipient: the operator+client dual-recipient
    install.yaml field is gone."""
    assert "client_age_pubkey" not in seed.load_input(None)


# --- VAULT_SKIP_KEYS (Community trim) ---------------------------------------
def test_vault_admin_password_is_skip_key(seed):
    assert "vault_admin_password" in seed.VAULT_SKIP_KEYS


def test_ce_service_keys_are_skip_keys(seed):
    for key in (
        "vault_keycloak_db_password",
        "vault_oauth2_proxy_cookie_secret",
        "vault_dashboard_sync_client_secret",
        "vault_healthchecks_secret_key",
        "vault_beszel_admin_password",
        "vault_mailserver_relay_password",
    ):
        assert key in seed.VAULT_SKIP_KEYS


def test_ee_and_operator_keys_dropped_from_skip_set(seed):
    """Operator-only (Semaphore), the operator's billing portal, bench-only
    pen-test, and the cold/WORM mirror secrets are not Community and must be
    gone from the skip set so seed never pre-populates dead keys."""
    for legacy in (
        "vault_semaphore_db_password",
        "vault_semaphore_admin_password",
        "vault_portal_db_password",
        "vault_portal_stripe_secret_key",
        "vault_portal_stripe_webhook_secret",
        "vault_zap_api_key",
        "vault_backup_worm_access_key",
        "vault_backup_worm_secret_key",
        "vault_nextcloud_worm_access_key",
    ):
        assert legacy not in seed.VAULT_SKIP_KEYS


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


def test_admin_password_constants(seed):
    assert seed.ADMIN_PASSWORD_MIN_LEN >= 16
    assert seed.ADMIN_PASSWORD_AUTO_LEN >= seed.ADMIN_PASSWORD_MIN_LEN


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


# --- emit_self_sops_yaml (single recipient) ---------------------------------
def test_emit_self_sops_yaml_single_recipient(seed, tmp_path):
    inv_dir = tmp_path / "inventory" / "prod"
    inv_dir.mkdir(parents=True)
    pub = "age1self000000000000000000000000000000000000000000000000000"
    seed.emit_self_sops_yaml(inv_dir, pub)
    written = yaml.safe_load((inv_dir / ".sops.yaml").read_text())
    rules = written["creation_rules"]
    assert len(rules) == 1
    assert "vault\\.sops" in rules[0]["path_regex"]
    assert rules[0]["key_groups"][0]["age"] == [pub]


def test_emit_self_sops_yaml_dies_without_pubkey(seed, tmp_path):
    inv_dir = tmp_path / "inventory" / "broken"
    inv_dir.mkdir(parents=True)
    with pytest.raises(SystemExit):
        seed.emit_self_sops_yaml(inv_dir, "")


# --- validate_install_structural --------------------------------------------
def _good_inp():
    return {
        "inventory": "prod",
        "host": {"name": "prod1", "public_ip": "203.0.113.10", "initial_user": "root"},
        "env": {"BACKUP_RESTIC_REPO": "s3:s3.example.net/mybucket-restic"},
        "vault": {
            "vault_tailscale_oauth_client_id": "x",
            "vault_tailscale_oauth_client_secret": "y",
            "vault_cloudflare_api_token": "z",
            "vault_backup_s3_access_key": "a",
            "vault_backup_s3_secret_key": "b",
        },
    }


_ENV_KEYS = [("BACKUP_RESTIC_REPO", "s3:s3.example-region.example.net/<client>-restic")]
_VAULT_KEYS = [
    "vault_tailscale_oauth_client_id",
    "vault_tailscale_oauth_client_secret",
    "vault_cloudflare_api_token",
    "vault_backup_s3_access_key",
    "vault_backup_s3_secret_key",
    "vault_dokploy_api_key",  # skip key -- not required
]


def test_validate_structural_clean(seed):
    assert seed.validate_install_structural(_good_inp(), _ENV_KEYS, _VAULT_KEYS) == 0


def test_validate_structural_missing_required_vault(seed):
    inp = _good_inp()
    del inp["vault"]["vault_cloudflare_api_token"]
    assert seed.validate_install_structural(inp, _ENV_KEYS, _VAULT_KEYS) >= 1


def test_validate_structural_blank_restic_repo(seed):
    inp = _good_inp()
    inp["env"]["BACKUP_RESTIC_REPO"] = ""
    assert seed.validate_install_structural(inp, _ENV_KEYS, _VAULT_KEYS) >= 1


def test_validate_structural_rejects_client_placeholder(seed):
    inp = _good_inp()
    inp["env"]["BACKUP_RESTIC_REPO"] = "s3:s3.example.net/<client>-restic"
    assert seed.validate_install_structural(inp, _ENV_KEYS, _VAULT_KEYS) >= 1


# --- _resolve_service_secrets (CE-only minting) -----------------------------
def test_service_secrets_mints_ce_groups(seed):
    v: dict[str, str] = {}
    seed._resolve_service_secrets(v, existing_vault=False)
    for key in (
        "vault_keycloak_db_password",
        "vault_oauth2_proxy_cookie_secret",
        "vault_dashboard_sync_client_secret",
        "vault_healthchecks_secret_key",
        "vault_dokploy_postgres_password",
        "vault_turn_static_auth_secret",
        "vault_beszel_admin_password",
    ):
        assert v.get(key), f"{key} should be minted"


def test_service_secrets_omits_operator_and_ee_groups(seed):
    v: dict[str, str] = {}
    seed._resolve_service_secrets(v, existing_vault=False)
    for key in (
        "vault_semaphore_db_password",
        "vault_portal_db_password",
        "vault_zap_api_key",
    ):
        assert key not in v, f"{key} must not be minted in Community"


def test_service_secrets_skips_existing_vault(seed):
    v: dict[str, str] = {}
    seed._resolve_service_secrets(v, existing_vault=True)
    assert v == {}


def test_oauth2_cookie_secret_decodes_to_32_bytes(seed):
    import base64
    secret = seed._mint_oauth2_proxy_cookie_secret()
    decoded = base64.urlsafe_b64decode(secret + "=" * (-len(secret) % 4))
    assert len(decoded) == 32
