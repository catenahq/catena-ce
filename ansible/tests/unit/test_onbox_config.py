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
    store = {"secrets": {"vault_catena_postgres_password": "keep-me"}, "config": {}}
    minted = oc.ensure_internal_secrets(store)
    assert "vault_catena_postgres_password" not in minted
    assert store["secrets"]["vault_catena_postgres_password"] == "keep-me"


def test_ensure_internal_replaces_blank(oc):
    store = {"secrets": {"vault_catena_postgres_password": "   "}, "config": {}}
    minted = oc.ensure_internal_secrets(store)
    assert "vault_catena_postgres_password" in minted
    assert store["secrets"]["vault_catena_postgres_password"].strip()


# --- per-zone (multi-domain) cookie secrets ---------------------------------
def test_zone_slug_and_cookie_key(oc):
    assert oc.zone_slug("Example.COM") == "example_com"
    assert oc.zone_slug("a-b.co.uk") == "a_b_co_uk"
    assert oc.zone_cookie_secret_key("example.com") == \
        "vault_oauth2_proxy_cookie_secret_example_com"


def test_configured_zone_names_accepts_list_and_json_dicts(oc):
    assert oc.configured_zone_names(["a.com", "b.com"]) == ["a.com", "b.com"]
    assert oc.configured_zone_names([{"zone": "a.com"}, {"zone": "b.com"}]) == \
        ["a.com", "b.com"]
    assert oc.configured_zone_names('[{"zone": "a.com"}]') == ["a.com"]
    assert oc.configured_zone_names(None) == []
    assert oc.configured_zone_names("not json") == []


def test_ensure_internal_mints_per_zone_cookie_secret(oc):
    store = {"secrets": {}, "config": {"CLOUDFLARE_ZONES": [
        {"zone": "a.com"}, {"zone": "b.com"}]}}
    minted = oc.ensure_internal_secrets(store)
    assert "vault_oauth2_proxy_cookie_secret_a_com" in minted
    assert "vault_oauth2_proxy_cookie_secret_b_com" in minted
    # per-zone secrets satisfy the same 32-byte decode contract.
    val = store["secrets"]["vault_oauth2_proxy_cookie_secret_a_com"]
    padded = val + "=" * (-len(val) % 4)
    assert len(base64.urlsafe_b64decode(padded)) == 32


def test_ensure_internal_per_zone_is_reconcile_not_overwrite(oc):
    store = {"secrets": {"vault_oauth2_proxy_cookie_secret_a_com": "keep-me"},
             "config": {"CLOUDFLARE_ZONES": [{"zone": "a.com"}]}}
    minted = oc.ensure_internal_secrets(store)
    assert "vault_oauth2_proxy_cookie_secret_a_com" not in minted
    assert store["secrets"]["vault_oauth2_proxy_cookie_secret_a_com"] == "keep-me"


def test_cloudflare_api_tokens_is_external(oc):
    assert "vault_cloudflare_api_tokens" in oc.EXTERNAL_SECRETS


def test_headscale_credentials_are_external(oc):
    # Headscale creds are client-supplied vendor creds, never on-box-minted.
    for key in ("vault_headscale_api_key", "vault_headscale_preauth_key"):
        assert key in oc.EXTERNAL_SECRETS
        assert key not in oc.INTERNAL_SECRETS
        assert key not in oc.USER_HELD_SECRETS


# --- minted-value format contracts (mirror seed.py) -------------------------
def test_hc_api_key_is_32_chars(oc):
    assert len(oc.mint_hc_api_key()) == 32


def test_cookie_secret_decodes_to_32_bytes(oc):
    val = oc.mint_oauth2_proxy_cookie_secret()
    # oauth2-proxy strips '=' padding then RawURLEncoding-decodes; must be 32B.
    padded = val + "=" * (-len(val) % 4)
    assert len(base64.urlsafe_b64decode(padded)) == 32


def test_strong_password_is_64_b64_chars(oc):
    assert len(oc.mint_strong_password()) == 64


# --- registries -------------------------------------------------------------
def _registries(oc) -> dict[str, set[str]]:
    return {
        "INTERNAL_SECRETS": set(oc.INTERNAL_SECRETS),
        "EXTERNAL_SECRETS": set(oc.EXTERNAL_SECRETS),
        "USER_HELD_SECRETS": set(oc.USER_HELD_SECRETS),
        "ROLE_MINTED_SECRETS": set(oc.ROLE_MINTED_SECRETS),
    }


def test_the_four_registries_are_pairwise_disjoint(oc):
    """A name in two registries has two provenances, which means the one the
    reader believes is a coin flip. Enumerated pairwise rather than by total
    length so the failure names the collision."""
    regs = _registries(oc)
    names = sorted(regs)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            assert not (regs[a] & regs[b]), f"{a} and {b} share {regs[a] & regs[b]}"


def test_every_registry_is_non_empty(oc):
    # A registry that parses empty would make the provenance gate vacuously
    # pass for everything it owns.
    for name, members in _registries(oc).items():
        assert members, f"{name} is empty"


def test_portainer_api_key_is_role_minted(oc):
    """Portainer mints its own API key: not internal (this module never
    generates it), not external (no human ever supplies one). It is
    ROLE_MINTED, which is a category with members rather than a comment
    saying it belongs to none."""
    assert oc.ROLE_MINTED_SECRETS["vault_portainer_api_key"] == "portainer"
    for reg in (oc.INTERNAL_SECRETS, oc.EXTERNAL_SECRETS, oc.USER_HELD_SECRETS):
        assert "vault_portainer_api_key" not in reg


def test_role_minted_secrets_are_not_settable_through_the_api(oc):
    # apply_inputs' allowlist is EXTERNAL_SECRETS; a role-minted secret has
    # exactly one writer and the settings form is not it.
    store = {"secrets": {}, "config": {}}
    with pytest.raises(ValueError):
        oc.apply_inputs(store, secrets_in={"vault_portainer_api_key": "x"})


def test_install_external_keys_are_a_subset_of_external_secrets(oc):
    """seed.py prompts for these at install and writes them to the transient
    adopt file; the loader hands them to apply_inputs, which RAISES on any key
    outside EXTERNAL_SECRETS. A prompt for a key not in the set is an install
    that aborts on its own input."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("seed", ANSIBLE_DIR / "seed.py")
    seed = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(seed)
    assert set(seed.INSTALL_EXTERNAL_KEYS) <= set(oc.EXTERNAL_SECRETS)


def test_dr_keyset_is_user_held_not_external(oc):
    """The admin password (first-login), restic backup password (DR keyset)
    and console break-glass password are USER_HELD: minted on-box if absent,
    shown once at install, but NOT EXTERNAL -- so the config-write API cannot
    set them, and ensure_internal does not mint them."""
    for key in ("vault_admin_password", "vault_backup_restic_password",
                "vault_console_recovery_password"):
        assert key in oc.USER_HELD_SECRETS
        assert key not in oc.EXTERNAL_SECRETS
        assert key not in oc.INTERNAL_SECRETS
    store = {"secrets": {}, "config": {}}
    assert "vault_admin_password" not in oc.ensure_internal_secrets(store)


def test_ensure_user_held_mints_the_whole_dr_keyset(oc):
    store = {"secrets": {}, "config": {}}
    minted = oc.ensure_user_held_secrets(store)
    assert set(minted) == {"vault_admin_password", "vault_backup_restic_password",
                           "vault_console_recovery_password"}
    # format contracts: admin 20 url-safe chars, restic 64 base64 chars.
    assert len(store["secrets"]["vault_admin_password"]) == 20
    assert len(store["secrets"]["vault_backup_restic_password"]) == 64


def test_console_recovery_password_is_console_typeable(oc):
    """It is entered at a provider KVM / serial console by hand. base64's
    '+' and '/' are a keyboard-layout hazard there; url-safe is not."""
    val = oc.USER_HELD_SECRETS["vault_console_recovery_password"]()
    assert len(val) == 20
    assert all(c.isalnum() or c in "-_" for c in val)


def test_cifs_bulk_credentials_are_external(oc):
    """roles/storage bulk.yml tells the client to enter these in catena-admin
    > Settings. apply_inputs RAISES for any key outside EXTERNAL_SECRETS, so
    absent from this set the documented path is closed by code."""
    for key in ("vault_storage_bulk_username", "vault_storage_bulk_password"):
        assert key in oc.EXTERNAL_SECRETS
        assert key not in oc.INTERNAL_SECRETS
        assert key not in oc.USER_HELD_SECRETS
    store = {"secrets": {}, "config": {}}
    oc.apply_inputs(store, secrets_in={"vault_storage_bulk_username": "svc"})
    assert store["secrets"]["vault_storage_bulk_username"] == "svc"


def test_ensure_user_held_does_not_overwrite_adopted(oc):
    """A restic password the user re-entered on `catena recover` (adopted first)
    is preserved; only a first install mints fresh."""
    store = {"secrets": {"vault_backup_restic_password": "user-saved"}, "config": {}}
    minted = oc.ensure_user_held_secrets(store)
    assert "vault_backup_restic_password" not in minted
    assert store["secrets"]["vault_backup_restic_password"] == "user-saved"


def test_admin_password_is_20_chars(oc):
    assert len(oc.mint_admin_password()) == 20


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
        oc.apply_inputs(store, secrets_in={"vault_catena_postgres_password": "smuggled"})


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


def test_adopt_overwrite_replaces_a_dead_value(oc):
    """roles/portainer re-mints when Portainer REJECTS the stored key. Without
    overwrite the store would keep serving the dead one and every API call
    would 401 for the rest of the converge."""
    store = {"secrets": {"vault_portainer_api_key": "revoked"}, "config": {}}
    adopted = oc.adopt(store, {"vault_portainer_api_key": "fresh"}, overwrite=True)
    assert adopted == ["vault_portainer_api_key"]
    assert store["secrets"]["vault_portainer_api_key"] == "fresh"


def test_adopt_overwrite_still_refuses_a_blank(oc):
    store = {"secrets": {"vault_portainer_api_key": "keep"}, "config": {}}
    assert oc.adopt(store, {"vault_portainer_api_key": "  "}, overwrite=True) == []
    assert store["secrets"]["vault_portainer_api_key"] == "keep"


def test_adopt_reports_no_change_when_the_value_is_identical(oc):
    """An unchanged re-adopt must not report a write: the portainer role's
    task reports changed from this, and a converge that changed nothing has
    to recap changed=0 for the bench idempotency gates."""
    store = {"secrets": {"vault_portainer_api_key": "same"}, "config": {}}
    assert oc.adopt(store, {"vault_portainer_api_key": "same"}, overwrite=True) == []


def test_cli_overwrite_reaches_adopt(oc, tmp_path, capsys, monkeypatch):
    import io
    p = tmp_path / "config.json"
    oc.dump({"secrets": {"vault_portainer_api_key": "revoked"}, "config": {}}, p)
    monkeypatch.setattr("sys.stdin", io.StringIO('{"vault_portainer_api_key": "fresh"}'))
    rc = oc.main(["--path", str(p), "--adopt-stdin", "--overwrite",
                  "--no-mint", "--emit", "none"])
    assert rc == 0
    assert capsys.readouterr().out == ""
    assert oc.load(p)["secrets"]["vault_portainer_api_key"] == "fresh"


def test_cli_adopt_without_overwrite_keeps_the_stored_value(oc, tmp_path, monkeypatch):
    import io
    p = tmp_path / "config.json"
    oc.dump({"secrets": {"vault_portainer_api_key": "stored"}, "config": {}}, p)
    monkeypatch.setattr("sys.stdin", io.StringIO('{"vault_portainer_api_key": "other"}'))
    assert oc.main(["--path", str(p), "--adopt-stdin", "--no-mint",
                    "--emit", "none"]) == 0
    assert oc.load(p)["secrets"]["vault_portainer_api_key"] == "stored"


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


def test_cli_adopt_file_then_mint(oc, tmp_path, capsys):
    p = tmp_path / "config.json"
    adopt = tmp_path / "adopt.json"
    adopt.write_text('{"vault_portainer_api_key": "ptr", "vault_admin_password": "adopted"}')
    rc = oc.main(["--path", str(p), "--adopt-file", str(adopt), "--emit", "secrets"])
    assert rc == 0
    emitted = json.loads(capsys.readouterr().out)
    assert emitted["vault_portainer_api_key"] == "ptr"
    assert emitted["vault_admin_password"] == "adopted"
    assert emitted["vault_catena_postgres_password"]  # minted


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
    assert emitted["vault_catena_postgres_password"]  # minted on-box
    on_disk = oc.load(p)
    assert on_disk["config"]["CLOUDFLARE_ZONE"] == "x.com"
    assert on_disk["secrets"]["vault_healthchecks_api_key_readonly"]


def test_dispatch_read_prints_store(oc, tmp_path, capsys, monkeypatch):
    import io
    p = tmp_path / "config.json"
    oc.dump({"secrets": {"vault_cloudflare_api_token": "cf"}, "config": {"BACKUP_RESTIC_REPO": "s3:x/y"}}, p)
    monkeypatch.setattr("sys.stdin", io.StringIO('{"op":"read"}'))
    rc = oc.main(["--path", str(p), "--dispatch-stdin"])
    assert rc == 0
    got = json.loads(capsys.readouterr().out)
    assert got["secrets"]["vault_cloudflare_api_token"] == "cf"
    assert got["config"]["BACKUP_RESTIC_REPO"] == "s3:x/y"


def test_dispatch_write_persists_without_minting(oc, tmp_path, capsys, monkeypatch):
    import io
    p = tmp_path / "config.json"
    req = '{"op":"write","secrets":{"vault_cloudflare_api_token":"cf"},"config":{"BACKUP_RESTIC_REPO":"s3:x/y"}}'
    monkeypatch.setattr("sys.stdin", io.StringIO(req))
    rc = oc.main(["--path", str(p), "--dispatch-stdin"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True}
    store = oc.load(p)
    assert store["secrets"]["vault_cloudflare_api_token"] == "cf"
    assert store["config"]["BACKUP_RESTIC_REPO"] == "s3:x/y"
    # write must NOT mint internal secrets (that happens at converge)
    assert "vault_catena_postgres_password" not in store["secrets"]


def test_dispatch_write_rejects_internal_secret(oc, tmp_path, monkeypatch):
    import io
    p = tmp_path / "config.json"
    monkeypatch.setattr("sys.stdin", io.StringIO('{"op":"write","secrets":{"vault_catena_postgres_password":"x"}}'))
    with pytest.raises(ValueError):
        oc.main(["--path", str(p), "--dispatch-stdin"])


def test_dispatch_write_rejects_restic_password(oc, tmp_path, monkeypatch):
    """Restic password is USER_HELD, not EXTERNAL: the settings config-write
    path must refuse it (a re-key is the dedicated restic action, not a save)."""
    import io
    p = tmp_path / "config.json"
    monkeypatch.setattr("sys.stdin", io.StringIO(
        '{"op":"write","secrets":{"vault_backup_restic_password":"x"}}'
    ))
    with pytest.raises(ValueError):
        oc.main(["--path", str(p), "--dispatch-stdin"])


def test_cli_mints_user_held_on_first_install(oc, tmp_path, capsys):
    """A fresh converge (no adopt) mints the admin + restic DR keyset on-box."""
    p = tmp_path / "config.json"
    rc = oc.main(["--path", str(p), "--set-config", "CLOUDFLARE_ZONE=x.com"])
    assert rc == 0
    store = oc.load(p)
    assert len(store["secrets"]["vault_admin_password"]) == 20
    assert len(store["secrets"]["vault_backup_restic_password"]) == 64


def test_cli_no_mint_seeds_only(oc, tmp_path, capsys):
    p = tmp_path / "config.json"
    rc = oc.main(["--path", str(p), "--set-secret", "vault_cloudflare_api_token=cf", "--no-mint"])
    assert rc == 0
    emitted = json.loads(capsys.readouterr().out)
    assert emitted == {"vault_cloudflare_api_token": "cf"}
