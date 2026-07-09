"""Unit tests for helpers/onbox_config.py -- the 0b on-box config store.

Covers: load/dump round-trip + 0600 mode, reconcile-not-overwrite minting,
minted-value format contracts (mirrors seed.py), external-vs-internal secret
separation, apply_inputs fill/overwrite/blank semantics, and the CLI."""
from __future__ import annotations

import base64
import importlib.util
import json
import stat
from pathlib import Path

import pytest

ANSIBLE_DIR = Path(__file__).resolve().parents[2]
MOD_PATH = ANSIBLE_DIR / "helpers" / "onbox_config.py"


@pytest.fixture(scope="module")
def oc():
    spec = importlib.util.spec_from_file_location("onbox_config", MOD_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- load / dump ------------------------------------------------------------
def test_load_absent_returns_empty_sections(oc, tmp_path):
    store = oc.load(tmp_path / "nope.json")
    assert store == {"secrets": {}, "config": {}}


def test_dump_then_load_roundtrip(oc, tmp_path):
    p = tmp_path / "config.json"
    store = {"secrets": {"vault_admin_password": "s3cr3t/+="}, "config": {"CLOUDFLARE_ZONE": "x.com"}}
    oc.dump(store, p)
    got = oc.load(p)
    assert got == store


def test_dump_is_0600(oc, tmp_path):
    p = tmp_path / "config.json"
    oc.dump({"secrets": {}, "config": {}}, p)
    mode = stat.S_IMODE(p.stat().st_mode)
    assert mode == 0o600, oct(mode)


def test_load_malformed_is_hard_error(oc, tmp_path):
    p = tmp_path / "config.json"
    p.write_text("[]")
    with pytest.raises(ValueError):
        oc.load(p)


# --- minting ----------------------------------------------------------------
def test_ensure_internal_mints_all_missing(oc):
    store = {"secrets": {}, "config": {}}
    minted = oc.ensure_internal_secrets(store)
    assert set(minted) == set(oc.INTERNAL_SECRETS)
    for key in oc.INTERNAL_SECRETS:
        assert store["secrets"][key]


def test_ensure_internal_is_idempotent(oc):
    store = {"secrets": {}, "config": {}}
    oc.ensure_internal_secrets(store)
    snapshot = dict(store["secrets"])
    minted2 = oc.ensure_internal_secrets(store)
    assert minted2 == []
    assert store["secrets"] == snapshot  # existing values untouched


def test_ensure_internal_does_not_overwrite(oc):
    store = {"secrets": {"vault_admin_password": "keep-me"}, "config": {}}
    minted = oc.ensure_internal_secrets(store)
    assert "vault_admin_password" not in minted
    assert store["secrets"]["vault_admin_password"] == "keep-me"


def test_ensure_internal_replaces_blank(oc):
    store = {"secrets": {"vault_admin_password": "   "}, "config": {}}
    minted = oc.ensure_internal_secrets(store)
    assert "vault_admin_password" in minted
    assert store["secrets"]["vault_admin_password"].strip()


# --- minted-value format contracts (mirror seed.py) -------------------------
def test_hc_api_key_is_32_chars(oc):
    assert len(oc.mint_hc_api_key()) == 32


def test_admin_password_is_20_chars(oc):
    assert len(oc.mint_admin_password()) == 20


def test_cookie_secret_decodes_to_32_bytes(oc):
    val = oc.mint_oauth2_proxy_cookie_secret()
    # oauth2-proxy strips '=' padding then RawURLEncoding-decodes; must be 32B.
    padded = val + "=" * (-len(val) % 4)
    assert len(base64.urlsafe_b64decode(padded)) == 32


def test_strong_password_is_64_b64_chars(oc):
    assert len(oc.mint_strong_password()) == 64


# --- registries -------------------------------------------------------------
def test_internal_and_external_are_disjoint(oc):
    assert not (set(oc.INTERNAL_SECRETS) & set(oc.EXTERNAL_SECRETS))


def test_portainer_api_key_in_neither_registry(oc):
    # Portainer mints its own API key; the store receives it from the role.
    assert "vault_portainer_api_key" not in oc.INTERNAL_SECRETS
    assert "vault_portainer_api_key" not in oc.EXTERNAL_SECRETS


# --- apply_inputs -----------------------------------------------------------
def test_apply_inputs_fills_missing_external(oc):
    store = {"secrets": {}, "config": {}}
    changed = oc.apply_inputs(store, secrets_in={"vault_cloudflare_api_token": "tok"})
    assert changed == ["vault_cloudflare_api_token"]
    assert store["secrets"]["vault_cloudflare_api_token"] == "tok"


def test_apply_inputs_fill_only_keeps_existing(oc):
    store = {"secrets": {"vault_cloudflare_api_token": "old"}, "config": {}}
    changed = oc.apply_inputs(store, secrets_in={"vault_cloudflare_api_token": "new"})
    assert changed == []
    assert store["secrets"]["vault_cloudflare_api_token"] == "old"


def test_apply_inputs_overwrite_replaces(oc):
    store = {"secrets": {"vault_cloudflare_api_token": "old"}, "config": {}}
    changed = oc.apply_inputs(store, secrets_in={"vault_cloudflare_api_token": "new"}, overwrite=True)
    assert changed == ["vault_cloudflare_api_token"]
    assert store["secrets"]["vault_cloudflare_api_token"] == "new"


def test_apply_inputs_blank_never_clears(oc):
    store = {"secrets": {"vault_cloudflare_api_token": "old"}, "config": {}}
    changed = oc.apply_inputs(store, secrets_in={"vault_cloudflare_api_token": ""}, overwrite=True)
    assert changed == []
    assert store["secrets"]["vault_cloudflare_api_token"] == "old"


def test_apply_inputs_rejects_internal_secret(oc):
    store = {"secrets": {}, "config": {}}
    with pytest.raises(ValueError):
        oc.apply_inputs(store, secrets_in={"vault_admin_password": "smuggled"})


def test_apply_inputs_config_fill_and_overwrite(oc):
    store = {"secrets": {}, "config": {"A": "1"}}
    assert oc.apply_inputs(store, config_in={"A": "2", "B": "3"}) == ["B"]
    assert store["config"] == {"A": "1", "B": "3"}
    assert oc.apply_inputs(store, config_in={"A": "2"}, overwrite=True) == ["A"]
    assert store["config"]["A"] == "2"


# --- adopt (migration capture) ----------------------------------------------
def test_adopt_fills_only_and_captures_any_key(oc):
    store = {"secrets": {"vault_admin_password": "keep"}, "config": {}}
    adopted = oc.adopt(store, {
        "vault_admin_password": "IGNORED-existing-wins",
        "vault_portainer_api_key": "ptr",       # out-of-registry, still captured
        "vault_cloudflare_api_token": "cf",
        "vault_blank": "   ",                    # blank skipped
    })
    assert set(adopted) == {"vault_portainer_api_key", "vault_cloudflare_api_token"}
    assert store["secrets"]["vault_admin_password"] == "keep"
    assert store["secrets"]["vault_portainer_api_key"] == "ptr"
    assert "vault_blank" not in store["secrets"]


def test_cli_adopt_stdin_then_mint(oc, tmp_path, capsys, monkeypatch):
    import io
    p = tmp_path / "config.json"
    monkeypatch.setattr("sys.stdin", io.StringIO(
        '{"vault_portainer_api_key": "ptr", "vault_admin_password": "adopted-admin"}'
    ))
    rc = oc.main(["--path", str(p), "--adopt-stdin", "--emit", "secrets"])
    assert rc == 0
    emitted = json.loads(capsys.readouterr().out)
    # adopted values survive; missing internal secrets get minted
    assert emitted["vault_portainer_api_key"] == "ptr"
    assert emitted["vault_admin_password"] == "adopted-admin"  # adopt beats mint
    assert emitted["vault_catena_postgres_password"]           # minted
    on_disk = oc.load(p)
    assert on_disk["secrets"]["vault_portainer_api_key"] == "ptr"


# --- CLI --------------------------------------------------------------------
def test_cli_seeds_external_mints_internal_and_emits(oc, tmp_path, capsys):
    p = tmp_path / "config.json"
    rc = oc.main([
        "--path", str(p),
        "--set-secret", "vault_cloudflare_api_token=cf",
        "--set-config", "CLOUDFLARE_ZONE=x.com",
    ])
    assert rc == 0
    emitted = json.loads(capsys.readouterr().out)
    # emits the full secret view (external + freshly minted internal)
    assert emitted["vault_cloudflare_api_token"] == "cf"
    assert emitted["vault_admin_password"]
    on_disk = oc.load(p)
    assert on_disk["config"]["CLOUDFLARE_ZONE"] == "x.com"
    assert on_disk["secrets"]["vault_healthchecks_api_key_readonly"]


def test_cli_no_mint_seeds_only(oc, tmp_path, capsys):
    p = tmp_path / "config.json"
    rc = oc.main(["--path", str(p), "--set-secret", "vault_cloudflare_api_token=cf", "--no-mint"])
    assert rc == 0
    emitted = json.loads(capsys.readouterr().out)
    assert emitted == {"vault_cloudflare_api_token": "cf"}
