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
    assert store == {"secrets": {}, "config": {}, oc.CLIENT_APP_SECRETS_KEY: {}}


def test_dump_then_load_roundtrip(oc, tmp_path):
    p = tmp_path / "config.json"
    store = {
        "secrets": {"admin_password": "s3cr3t/+="},
        "config": {"CLOUDFLARE_ZONE": "x.com"},
        oc.CLIENT_APP_SECRETS_KEY: {"kimai/DB_PASSWORD": "abc"},
    }
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
    store = {"secrets": {"catena_postgres_password": "keep-me"}, "config": {}}
    minted = oc.ensure_internal_secrets(store)
    assert "catena_postgres_password" not in minted
    assert store["secrets"]["catena_postgres_password"] == "keep-me"


def test_ensure_internal_replaces_blank(oc):
    store = {"secrets": {"catena_postgres_password": "   "}, "config": {}}
    minted = oc.ensure_internal_secrets(store)
    assert "catena_postgres_password" in minted
    assert store["secrets"]["catena_postgres_password"].strip()


# --- per-zone (multi-domain) cookie secrets ---------------------------------
def test_zone_slug_and_cookie_key(oc):
    assert oc.zone_slug("Example.COM") == "example_com"
    assert oc.zone_slug("a-b.co.uk") == "a_b_co_uk"
    assert oc.zone_cookie_secret_key("example.com") == \
        "oauth2_proxy_cookie_secret_example_com"


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
    assert "oauth2_proxy_cookie_secret_a_com" in minted
    assert "oauth2_proxy_cookie_secret_b_com" in minted
    # per-zone secrets satisfy the same 32-byte decode contract.
    val = store["secrets"]["oauth2_proxy_cookie_secret_a_com"]
    padded = val + "=" * (-len(val) % 4)
    assert len(base64.urlsafe_b64decode(padded)) == 32


def test_ensure_internal_per_zone_is_reconcile_not_overwrite(oc):
    store = {"secrets": {"oauth2_proxy_cookie_secret_a_com": "keep-me"},
             "config": {"CLOUDFLARE_ZONES": [{"zone": "a.com"}]}}
    minted = oc.ensure_internal_secrets(store)
    assert "oauth2_proxy_cookie_secret_a_com" not in minted
    assert store["secrets"]["oauth2_proxy_cookie_secret_a_com"] == "keep-me"


def test_cloudflare_api_tokens_is_external(oc):
    assert "cloudflare_api_tokens" in oc.EXTERNAL_SECRETS


def test_headscale_credentials_are_external(oc):
    # Headscale creds are client-supplied vendor creds, never on-box-minted.
    for key in ("headscale_api_key", "headscale_preauth_key"):
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


# --- secret_names: the converge loader's discriminator ----------------------
def test_secret_names_is_the_union_of_the_four_registries(oc):
    """playbooks/tasks/load_onbox_config.yml reads this list to decide which
    in-scope Ansible variables to capture into the store. Deciding that with a
    regex such as ^vault_.+$ would make a name PREFIX load-bearing, capturing a
    variable for how it is spelled rather than because someone declared it a
    secret. The union of the four registries is the declaration."""
    expected = set().union(*_registries(oc).values())
    assert set(oc.secret_names()) == expected


def test_secret_names_is_sorted_and_unique(oc):
    """The loader joins these into a regex alternation. Duplicate or
    unordered output would still work but makes a converge diff noisy for no
    reason."""
    names = oc.secret_names()
    assert names == sorted(set(names))


def test_secret_names_emit_touches_no_store(oc, tmp_path, capsys):
    """A fresh box runs this BEFORE the store exists. Answering it must not
    create the file, and must not mint anything into it."""
    store_path = tmp_path / "config.json"
    rc = oc.main(["--path", str(store_path), "--emit", "secret-names"])
    assert rc == 0
    assert not store_path.exists(), "the query wrote a store"
    printed = json.loads(capsys.readouterr().out)
    assert printed == oc.secret_names()


def test_portainer_api_key_is_role_minted(oc):
    """Portainer mints its own API key: not internal (this module never
    generates it), not external (no human ever supplies one). It is
    ROLE_MINTED, which is a category with members rather than a comment
    saying it belongs to none."""
    assert oc.ROLE_MINTED_SECRETS["portainer_api_key"] == "portainer"
    for reg in (oc.INTERNAL_SECRETS, oc.EXTERNAL_SECRETS, oc.USER_HELD_SECRETS):
        assert "portainer_api_key" not in reg


def test_role_minted_secrets_are_not_settable_through_the_api(oc):
    # apply_inputs' allowlist is EXTERNAL_SECRETS; a role-minted secret has
    # exactly one writer and the settings form is not it.
    store = {"secrets": {}, "config": {}}
    with pytest.raises(ValueError):
        oc.apply_inputs(store, secrets_in={"portainer_api_key": "x"})


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
    for key in ("admin_password", "backup_restic_password",
                "console_recovery_password"):
        assert key in oc.USER_HELD_SECRETS
        assert key not in oc.EXTERNAL_SECRETS
        assert key not in oc.INTERNAL_SECRETS
    store = {"secrets": {}, "config": {}}
    assert "admin_password" not in oc.ensure_internal_secrets(store)


def test_ensure_user_held_mints_the_whole_dr_keyset(oc):
    store = {"secrets": {}, "config": {}}
    minted = oc.ensure_user_held_secrets(store)
    assert set(minted) == {"admin_password", "backup_restic_password",
                           "console_recovery_password"}
    # format contracts: admin 20 url-safe chars, restic 64 base64 chars.
    assert len(store["secrets"]["admin_password"]) == 20
    assert len(store["secrets"]["backup_restic_password"]) == 64


def test_console_recovery_password_is_console_typeable(oc):
    """It is entered at a provider KVM / serial console by hand. base64's
    '+' and '/' are a keyboard-layout hazard there; url-safe is not."""
    val = oc.USER_HELD_SECRETS["console_recovery_password"]()
    assert len(val) == 20
    assert all(c.isalnum() or c in "-_" for c in val)


def test_cifs_bulk_credentials_are_external(oc):
    """bootstrap/roles/storage bulk.yml tells the client to enter these in catena-admin
    > Settings. apply_inputs RAISES for any key outside EXTERNAL_SECRETS, so
    absent from this set the documented path is closed by code."""
    for key in ("storage_bulk_username", "storage_bulk_password"):
        assert key in oc.EXTERNAL_SECRETS
        assert key not in oc.INTERNAL_SECRETS
        assert key not in oc.USER_HELD_SECRETS
    store = {"secrets": {}, "config": {}}
    oc.apply_inputs(store, secrets_in={"storage_bulk_username": "svc"})
    assert store["secrets"]["storage_bulk_username"] == "svc"


def test_ensure_user_held_does_not_overwrite_adopted(oc):
    """A restic password the user re-entered on `catena recover` (adopted first)
    is preserved; only a first install mints fresh."""
    store = {"secrets": {"backup_restic_password": "user-saved"}, "config": {}}
    minted = oc.ensure_user_held_secrets(store)
    assert "backup_restic_password" not in minted
    assert store["secrets"]["backup_restic_password"] == "user-saved"


def test_admin_password_is_20_chars(oc):
    assert len(oc.mint_admin_password()) == 20


# --- apply_inputs -----------------------------------------------------------
def test_apply_inputs_fills_missing_external(oc):
    store = {"secrets": {}, "config": {}}
    changed = oc.apply_inputs(store, secrets_in={"cloudflare_api_token": "tok"})
    assert changed == ["cloudflare_api_token"]
    assert store["secrets"]["cloudflare_api_token"] == "tok"


def test_apply_inputs_fill_only_keeps_existing(oc):
    store = {"secrets": {"cloudflare_api_token": "old"}, "config": {}}
    changed = oc.apply_inputs(store, secrets_in={"cloudflare_api_token": "new"})
    assert changed == []
    assert store["secrets"]["cloudflare_api_token"] == "old"


def test_apply_inputs_overwrite_replaces(oc):
    store = {"secrets": {"cloudflare_api_token": "old"}, "config": {}}
    changed = oc.apply_inputs(store, secrets_in={"cloudflare_api_token": "new"}, overwrite=True)
    assert changed == ["cloudflare_api_token"]
    assert store["secrets"]["cloudflare_api_token"] == "new"


def test_apply_inputs_blank_never_clears(oc):
    store = {"secrets": {"cloudflare_api_token": "old"}, "config": {}}
    changed = oc.apply_inputs(store, secrets_in={"cloudflare_api_token": ""}, overwrite=True)
    assert changed == []
    assert store["secrets"]["cloudflare_api_token"] == "old"


def test_apply_inputs_rejects_internal_secret(oc):
    store = {"secrets": {}, "config": {}}
    with pytest.raises(ValueError):
        oc.apply_inputs(store, secrets_in={"catena_postgres_password": "smuggled"})


def test_apply_inputs_config_fill_and_overwrite(oc):
    store = {"secrets": {}, "config": {"A": "1"}}
    assert oc.apply_inputs(store, config_in={"A": "2", "B": "3"}) == ["B"]
    assert store["config"] == {"A": "1", "B": "3"}
    assert oc.apply_inputs(store, config_in={"A": "2"}, overwrite=True) == ["A"]
    assert store["config"]["A"] == "2"


# --- adopt (migration capture) ----------------------------------------------
def test_adopt_fills_only_and_captures_any_key(oc):
    store = {"secrets": {"admin_password": "keep"}, "config": {}}
    adopted = oc.adopt(store, {
        "admin_password": "IGNORED-existing-wins",
        "portainer_api_key": "ptr",       # out-of-registry, still captured
        "cloudflare_api_token": "cf",
        "vault_blank": "   ",                    # blank skipped
    })
    assert set(adopted) == {"portainer_api_key", "cloudflare_api_token"}
    assert store["secrets"]["admin_password"] == "keep"
    assert store["secrets"]["portainer_api_key"] == "ptr"
    assert "vault_blank" not in store["secrets"]


def test_adopt_overwrite_replaces_a_dead_value(oc):
    """reconcile/roles/portainer re-mints when Portainer REJECTS the stored key. Without
    overwrite the store would keep serving the dead one and every API call
    would 401 for the rest of the converge."""
    store = {"secrets": {"portainer_api_key": "revoked"}, "config": {}}
    adopted = oc.adopt(store, {"portainer_api_key": "fresh"}, overwrite=True)
    assert adopted == ["portainer_api_key"]
    assert store["secrets"]["portainer_api_key"] == "fresh"


def test_adopt_overwrite_still_refuses_a_blank(oc):
    store = {"secrets": {"portainer_api_key": "keep"}, "config": {}}
    assert oc.adopt(store, {"portainer_api_key": "  "}, overwrite=True) == []
    assert store["secrets"]["portainer_api_key"] == "keep"


def test_adopt_reports_no_change_when_the_value_is_identical(oc):
    """An unchanged re-adopt must not report a write: the portainer role's
    task reports changed from this, and a converge that changed nothing has
    to recap changed=0 for the bench idempotency gates."""
    store = {"secrets": {"portainer_api_key": "same"}, "config": {}}
    assert oc.adopt(store, {"portainer_api_key": "same"}, overwrite=True) == []


def test_cli_overwrite_reaches_adopt(oc, tmp_path, capsys, monkeypatch):
    import io
    p = tmp_path / "config.json"
    oc.dump({"secrets": {"portainer_api_key": "revoked"}, "config": {}}, p)
    monkeypatch.setattr("sys.stdin", io.StringIO('{"portainer_api_key": "fresh"}'))
    rc = oc.main(["--path", str(p), "--adopt-stdin", "--overwrite",
                  "--no-mint", "--emit", "none"])
    assert rc == 0
    assert capsys.readouterr().out == ""
    assert oc.load(p)["secrets"]["portainer_api_key"] == "fresh"


def test_cli_adopt_without_overwrite_keeps_the_stored_value(oc, tmp_path, monkeypatch):
    import io
    p = tmp_path / "config.json"
    oc.dump({"secrets": {"portainer_api_key": "stored"}, "config": {}}, p)
    monkeypatch.setattr("sys.stdin", io.StringIO('{"portainer_api_key": "other"}'))
    assert oc.main(["--path", str(p), "--adopt-stdin", "--no-mint",
                    "--emit", "none"]) == 0
    assert oc.load(p)["secrets"]["portainer_api_key"] == "stored"


def test_cli_adopt_stdin_then_mint(oc, tmp_path, capsys, monkeypatch):
    import io
    p = tmp_path / "config.json"
    monkeypatch.setattr("sys.stdin", io.StringIO(
        '{"portainer_api_key": "ptr", "admin_password": "adopted-admin"}'
    ))
    rc = oc.main(["--path", str(p), "--adopt-stdin", "--emit", "secrets"])
    assert rc == 0
    emitted = json.loads(capsys.readouterr().out)
    # adopted values survive; missing internal secrets get minted
    assert emitted["portainer_api_key"] == "ptr"
    assert emitted["admin_password"] == "adopted-admin"  # adopt beats mint
    assert emitted["catena_postgres_password"]           # minted
    on_disk = oc.load(p)
    assert on_disk["secrets"]["portainer_api_key"] == "ptr"


def test_cli_adopt_file_then_mint(oc, tmp_path, capsys):
    p = tmp_path / "config.json"
    adopt = tmp_path / "adopt.json"
    adopt.write_text('{"portainer_api_key": "ptr", "admin_password": "adopted"}')
    rc = oc.main(["--path", str(p), "--adopt-file", str(adopt), "--emit", "secrets"])
    assert rc == 0
    emitted = json.loads(capsys.readouterr().out)
    assert emitted["portainer_api_key"] == "ptr"
    assert emitted["admin_password"] == "adopted"
    assert emitted["catena_postgres_password"]  # minted


# --- CLI --------------------------------------------------------------------
def test_cli_seeds_external_mints_internal_and_emits(oc, tmp_path, capsys):
    p = tmp_path / "config.json"
    rc = oc.main([
        "--path", str(p),
        "--set-secret", "cloudflare_api_token=cf",
        "--set-config", "CLOUDFLARE_ZONE=x.com",
    ])
    assert rc == 0
    emitted = json.loads(capsys.readouterr().out)
    # emits the full secret view (external + freshly minted internal)
    assert emitted["cloudflare_api_token"] == "cf"
    assert emitted["catena_postgres_password"]  # minted on-box
    on_disk = oc.load(p)
    assert on_disk["config"]["CLOUDFLARE_ZONE"] == "x.com"
    assert on_disk["secrets"]["healthchecks_api_key_readonly"]


def test_dispatch_read_prints_store(oc, tmp_path, capsys, monkeypatch):
    import io
    p = tmp_path / "config.json"
    oc.dump({"secrets": {"cloudflare_api_token": "cf"}, "config": {"BACKUP_RESTIC_REPO": "s3:x/y"}}, p)
    monkeypatch.setattr("sys.stdin", io.StringIO('{"op":"read"}'))
    rc = oc.main(["--path", str(p), "--dispatch-stdin"])
    assert rc == 0
    got = json.loads(capsys.readouterr().out)
    assert got["secrets"]["cloudflare_api_token"] == "cf"
    assert got["config"]["BACKUP_RESTIC_REPO"] == "s3:x/y"


def test_dispatch_write_persists_without_minting(oc, tmp_path, capsys, monkeypatch):
    import io
    p = tmp_path / "config.json"
    req = '{"op":"write","secrets":{"cloudflare_api_token":"cf"},"config":{"BACKUP_RESTIC_REPO":"s3:x/y"}}'
    monkeypatch.setattr("sys.stdin", io.StringIO(req))
    rc = oc.main(["--path", str(p), "--dispatch-stdin"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True}
    store = oc.load(p)
    assert store["secrets"]["cloudflare_api_token"] == "cf"
    assert store["config"]["BACKUP_RESTIC_REPO"] == "s3:x/y"
    # write must NOT mint internal secrets (that happens at converge)
    assert "catena_postgres_password" not in store["secrets"]


def test_dispatch_write_rejects_internal_secret(oc, tmp_path, monkeypatch):
    import io
    p = tmp_path / "config.json"
    monkeypatch.setattr("sys.stdin", io.StringIO('{"op":"write","secrets":{"catena_postgres_password":"x"}}'))
    with pytest.raises(ValueError):
        oc.main(["--path", str(p), "--dispatch-stdin"])


def test_dispatch_write_rejects_restic_password(oc, tmp_path, monkeypatch):
    """Restic password is USER_HELD, not EXTERNAL: the settings config-write
    path must refuse it (a re-key is the dedicated restic action, not a save)."""
    import io
    p = tmp_path / "config.json"
    monkeypatch.setattr("sys.stdin", io.StringIO(
        '{"op":"write","secrets":{"backup_restic_password":"x"}}'
    ))
    with pytest.raises(ValueError):
        oc.main(["--path", str(p), "--dispatch-stdin"])


# --- client-app env secrets (the marketplace's per-deploy values) -----------
def _wanted(key="kimai/DB_PASSWORD", length=32, charset="alnum"):
    return {key: {"length": length, "charset": charset}}


def test_app_secrets_mint_then_reuse(oc):
    """Asking twice returns the SAME value. The catalog is re-rendered on every
    marketplace fetch; a mint per render would hand the client a different
    database password each time they opened the page."""
    store = {"secrets": {}, "config": {}}
    first = oc.ensure_app_secrets(store, _wanted())
    second = oc.ensure_app_secrets(store, _wanted())
    assert first == second
    assert len(first["kimai/DB_PASSWORD"]) == 32


def test_app_secrets_charsets(oc):
    store = {"secrets": {}, "config": {}}
    got = oc.ensure_app_secrets(store, {
        "outline/OUTLINE_SECRET_KEY": {"length": 64, "charset": "hex"},
        "kimai/DB_PASSWORD": {"length": 32, "charset": "alnum"},
    })
    assert set(got["outline/OUTLINE_SECRET_KEY"]) <= set("0123456789abcdef")
    assert got["kimai/DB_PASSWORD"].isalnum()


def test_app_secrets_blank_is_reminted(oc):
    """A blank stored value is not a value -- same reconcile rule the internal
    set follows, and the state a hand-edited store can be left in."""
    store = {"secrets": {}, "config": {}, oc.CLIENT_APP_SECRETS_KEY: {"kimai/DB_PASSWORD": "  "}}
    got = oc.ensure_app_secrets(store, _wanted())
    assert got["kimai/DB_PASSWORD"].strip()


@pytest.mark.parametrize("bad", [
    "../../etc/passwd/KEY",
    "kimai/db_password",
    "Kimai/DB_PASSWORD",
    "kimai DB_PASSWORD",
    "kimai/",
])
def test_app_secrets_rejects_bad_key(oc, bad):
    """This is the one store write the unprivileged container can ask for by
    name, so the key shape is what keeps a forged request inside the block."""
    with pytest.raises(ValueError):
        oc.ensure_app_secrets({"secrets": {}, "config": {}}, _wanted(key=bad))


@pytest.mark.parametrize("length", [0, 8, 257, "32", True, None])
def test_app_secrets_rejects_bad_length(oc, length):
    with pytest.raises(ValueError):
        oc.ensure_app_secrets({"secrets": {}, "config": {}}, _wanted(length=length))


def test_app_secrets_rejects_unknown_charset(oc):
    with pytest.raises(ValueError):
        oc.ensure_app_secrets({"secrets": {}, "config": {}}, _wanted(charset="base64"))


def test_app_secrets_never_takes_a_value_from_the_request(oc):
    """The request names what it wants, never what it should be. A caller that
    could supply the value could pin every client's password to one string."""
    store = {"secrets": {}, "config": {}}
    got = oc.ensure_app_secrets(store, {
        "kimai/DB_PASSWORD": {"length": 32, "charset": "alnum", "value": "chosen"},
    })
    assert got["kimai/DB_PASSWORD"] != "chosen"


def test_dispatch_mint_app_secrets_persists_and_returns(oc, tmp_path, monkeypatch):
    import io
    p = tmp_path / "config.json"
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({
        "op": "mint-app-secrets",
        "app_secrets": {"kimai/DB_PASSWORD": {"length": 32, "charset": "alnum"}},
    })))
    rc = oc.main(["--path", str(p), "--dispatch-stdin"])
    assert rc == 0
    stored = oc.client_app_secrets(p)
    assert len(stored["kimai/DB_PASSWORD"]) == 32


def test_dispatch_mint_app_secrets_preserves_other_blocks(oc, tmp_path, monkeypatch):
    """The store is shared. catena-schedule owns `schedules`, the update lane
    owns `image_pins`, and a mint must carry both past untouched."""
    import io
    p = tmp_path / "config.json"
    p.write_text(json.dumps({
        "secrets": {"admin_password": "keep"},
        "config": {"CLOUDFLARE_ZONE": "x.com"},
        "image_pins": {"catena/thing": "sha256:abc"},
        "schedules": {"backup": "daily"},
    }))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({
        "op": "mint-app-secrets",
        "app_secrets": {"kimai/DB_PASSWORD": {"length": 32, "charset": "alnum"}},
    })))
    assert oc.main(["--path", str(p), "--dispatch-stdin"]) == 0
    doc = json.loads(p.read_text())
    assert doc["image_pins"] == {"catena/thing": "sha256:abc"}
    assert doc["schedules"] == {"backup": "daily"}
    assert doc["secrets"]["admin_password"] == "keep"


def test_converge_style_dump_does_not_blank_app_secrets(oc, tmp_path):
    """The converge loader dumps a hand-built {"secrets", "config"} store. It
    has no opinion about client_app_secrets and must not erase it -- the same
    trap that made dump() stop re-serialising the whole document."""
    p = tmp_path / "config.json"
    p.write_text(json.dumps({
        "secrets": {}, "config": {},
        oc.CLIENT_APP_SECRETS_KEY: {"kimai/DB_PASSWORD": "survives"},
    }))
    oc.dump({"secrets": {"a": "b"}, "config": {}}, p)
    assert oc.client_app_secrets(p) == {"kimai/DB_PASSWORD": "survives"}


def test_client_app_secrets_absent_store_is_empty(oc, tmp_path):
    assert oc.client_app_secrets(tmp_path / "nope.json") == {}


def test_cli_mints_user_held_on_first_install(oc, tmp_path, capsys):
    """A fresh converge (no adopt) mints the admin + restic DR keyset on-box."""
    p = tmp_path / "config.json"
    rc = oc.main(["--path", str(p), "--set-config", "CLOUDFLARE_ZONE=x.com"])
    assert rc == 0
    store = oc.load(p)
    assert len(store["secrets"]["admin_password"]) == 20
    assert len(store["secrets"]["backup_restic_password"]) == 64


def test_cli_no_mint_seeds_only(oc, tmp_path, capsys):
    p = tmp_path / "config.json"
    rc = oc.main(["--path", str(p), "--set-secret", "cloudflare_api_token=cf", "--no-mint"])
    assert rc == 0
    emitted = json.loads(capsys.readouterr().out)
    assert emitted == {"cloudflare_api_token": "cf"}


# --- non-secret config ownership (SECRETS.md category 4) --------------------
def test_every_config_key_has_exactly_one_owner(oc):
    """Belonging to neither set is not a state. That is what let the .env and
    the store both be live readers of the same key -- run-backup.sh reconciled
    them in shell, and retention ended up with two copies, one silently dead."""
    overlap = set(oc.SETTINGS_CONFIG) & oc.BOOTSTRAP_CONFIG
    assert not overlap, f"claimed by both owners: {sorted(overlap)}"


def test_every_dotenv_key_the_converge_reads_is_declared(oc):
    """The registry has to cover what the tree actually reads, or a new key
    quietly acquires a third owner: nobody.

    inventory/ is excluded for the same reason its sibling test below excludes
    it: it IS the seed surface, not a reader of it. Only `skel/hosts.yml.example`
    is tracked, and its `.example` suffix kept it out of the scan by accident --
    so the first real `inventory/<name>/hosts.yml` on any developer's machine
    failed this gate on keys (HOST_PUBLIC_IP, HOST_SSH_PORT, HOST_INITIAL_USER)
    that are inventory-only by design and have no on-box owner to declare."""
    import re
    root = ANSIBLE_DIR
    pattern = re.compile(r"lookup\('dotenv',\s*'([A-Z0-9_]+)'")
    known = set(oc.SETTINGS_CONFIG) | oc.BOOTSTRAP_CONFIG
    undeclared: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        s = str(path)
        if not path.is_file() or path.suffix not in {".yml", ".yaml", ".j2"}:
            continue
        if ".collections" in s or "/tests/" in s or "/inventory/" in s:
            continue
        for key in pattern.findall(path.read_text()):
            if key not in known:
                undeclared.setdefault(key, s)
    assert not undeclared, (
        "config keys read from .env with no declared owner in "
        f"onbox_config.py: {undeclared}"
    )


def test_no_settings_key_is_read_from_dotenv_outside_the_seed(oc):
    """The whole point: post-install config has ONE live reader. The inventory
    and the loader are the seed surface; a role default reading .env directly
    is the silent-fallback bug class returning."""
    import re
    pattern = re.compile(r"lookup\('dotenv',\s*'([A-Z0-9_]+)'")
    offenders: dict[str, str] = {}
    for path in sorted(ANSIBLE_DIR.rglob("*")):
        s = str(path)
        if not path.is_file() or path.suffix not in {".yml", ".yaml", ".j2"}:
            continue
        if ".collections" in s or "/tests/" in s or "/inventory/" in s:
            continue
        if path.name == "load_onbox_config.yml":
            continue  # the seeding task, by construction
        if path.name == "preflight.yml":
            # Runs on the CONTROLLER, before there is a host, and therefore
            # before there is a store to read. Its one .env read is the tailnet
            # control URL, which it hands to an advisory check that answers
            # "is this laptop on the tailnet it is about to converge" -- a
            # question asked of the inventory, about a machine that does not
            # exist yet. It is seed surface, not a second live reader.
            continue
        for key in pattern.findall(path.read_text()):
            if key in oc.SETTINGS_CONFIG:
                offenders.setdefault(key, s)
    assert not offenders, f"settings keys still read from .env: {offenders}"


def test_settings_config_vars_projects_only_present_keys(oc):
    got = oc.settings_config_vars({"NTFY_SERVER": "https://n.example"})
    assert got == {"cfg_ntfy_server": "https://n.example"}


def test_settings_config_vars_ignores_unknown_keys(oc):
    """The settings API accepts forward keys a given catena-ce may not know
    yet; a converge must not invent a fact from one."""
    assert oc.settings_config_vars({"SOME_FUTURE_KEY": "x"}) == {}
    assert oc.settings_config_vars({}) == {}
    assert oc.settings_config_vars(None) == {}


def test_projected_var_names_are_prefixed(oc):
    """cfg_ is not decoration. keycloak/defaults derives smtp_host from the
    Resend/Brevo autofill, so a fact literally named smtp_host would silently
    replace the derivation with the raw value."""
    for key, var in oc.SETTINGS_CONFIG.items():
        assert var.startswith("cfg_"), f"{key} projects onto {var}"


def test_seeding_is_fill_only(oc, tmp_path):
    """The .env seeds a fresh store and never again. A converge that
    overwrote would undo every settings-page edit on the next run."""
    p = tmp_path / "config.json"
    store = oc.load(p)
    oc.apply_inputs(store, config_in={"NTFY_SERVER": "https://seed.example"})
    oc.dump(store, p)

    store = oc.load(p)
    store["config"]["NTFY_SERVER"] = "https://client-chose.example"
    oc.dump(store, p)

    store = oc.load(p)
    oc.apply_inputs(store, config_in={"NTFY_SERVER": "https://seed.example"})
    assert store["config"]["NTFY_SERVER"] == "https://client-chose.example"


def test_a_settings_write_overwrites(oc, tmp_path):
    """The panel edit path uses overwrite=True, which is what makes the store
    the owner rather than a cache of the first value seen."""
    store = {"secrets": {}, "config": {"NTFY_SERVER": "https://old.example"}}
    oc.apply_inputs(store, config_in={"NTFY_SERVER": "https://new.example"},
                    overwrite=True)
    assert store["config"]["NTFY_SERVER"] == "https://new.example"


def test_cli_emits_the_settings_key_names_without_touching_the_store(oc, tmp_path, capsys):
    """The loader asks for this before the store exists on a fresh box."""
    p = tmp_path / "config.json"
    rc = oc.main(["--path", str(p), "--emit", "settings-config-names"])
    assert rc == 0
    assert not p.exists(), "a pure query must not create the store"
    assert "BACKUP_RESTIC_REPO" in json.loads(capsys.readouterr().out)


def test_cli_emits_config_vars(oc, tmp_path, capsys):
    p = tmp_path / "config.json"
    oc.dump({"secrets": {}, "config": {"SMTP_HOST": "mail.example"}}, p)
    rc = oc.main(["--path", str(p), "--no-mint", "--emit", "config-vars"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {"cfg_smtp_host": "mail.example"}


# --- other writers' keys ----------------------------------------------------
#
# This helper owns `secrets` and `config`. Two other writers share the file:
# catena-schedule owns `schedules` and `backup_retention`, and the on-host
# update lane owns `image_pins`. Serialising only the two keys it knows about
# deleted the others on every converge and on every panel save.

def test_a_converge_keeps_the_keys_other_engines_wrote(oc, tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({
        "secrets": {"admin_password": "old"},
        "config": {"CLOUDFLARE_ZONE": "x.com"},
        "schedules": {"backup": "weekly"},
        "backup_retention": {"keep_daily": 7},
        "image_pins": {"traefik": "traefik:v3.7.12"},
    }))

    store = oc.load(p)
    store["secrets"]["admin_password"] = "new"
    oc.dump(store, p)

    got = json.loads(p.read_text())
    assert got["secrets"]["admin_password"] == "new"
    assert got["schedules"] == {"backup": "weekly"}, \
        "catena-schedule's block was wiped by a converge"
    assert got["image_pins"] == {"traefik": "traefik:v3.7.12"}, (
        "the image pins were wiped by the very converge that reads them, so "
        "every managed bump is reverted no matter what the lane recorded")


def test_a_corrupt_store_is_not_silently_replaced(oc, tmp_path):
    p = tmp_path / "config.json"
    p.write_text("{not json")
    with pytest.raises(Exception):
        oc.dump({"secrets": {}, "config": {}}, p)
    assert p.read_text() == "{not json"


# --- image pins -------------------------------------------------------------

def test_image_pins_reads_what_the_lane_wrote(oc, tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"image_pins": {"traefik": "traefik:v3.7.12"}}))
    assert oc.image_pins(p) == {"traefik": "traefik:v3.7.12"}


def test_image_pins_is_empty_on_a_host_that_has_never_bumped(oc, tmp_path):
    p = tmp_path / "config.json"
    assert oc.image_pins(p) == {}, "a fresh host must converge, not fail"
    p.write_text(json.dumps({"secrets": {}, "config": {}}))
    assert oc.image_pins(p) == {}
    p.write_text(json.dumps({"image_pins": "not-an-object"}))
    assert oc.image_pins(p) == {}


def test_cli_emits_image_pins_without_touching_the_store(oc, tmp_path, capsys):
    """Read before reconcile/roles/payload has necessarily installed anything, so it must
    not mint, must not write, and must answer on a host with no store."""
    p = tmp_path / "config.json"
    rc = oc.main(["--path", str(p), "--emit", "image-pins"])
    assert rc == 0
    assert not p.exists(), "a pure query must not create the store"
    assert json.loads(capsys.readouterr().out) == {}
