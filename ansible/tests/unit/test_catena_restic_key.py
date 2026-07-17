"""Unit tests for scripts/catena-restic-key.py -- the backup-password
validate/rotate helper. restic is stubbed (a fake on PATH) so no real repo is
touched; we assert the request routing, the store read, and the persistence on
rotate."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import stat
from pathlib import Path

import pytest

ANSIBLE_DIR = Path(__file__).resolve().parents[2]
MOD_PATH = ANSIBLE_DIR / "scripts" / "catena-restic-key.py"


@pytest.fixture(scope="module")
def rk():
    spec = importlib.util.spec_from_file_location("catena_restic_key", MOD_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _store(tmp_path, **secrets):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({
        "secrets": {"vault_backup_s3_access_key": "ak",
                    "vault_backup_s3_secret_key": "sk", **secrets},
        "config": {"BACKUP_RESTIC_REPO": "s3:ep/bucket"},
    }))
    return p


def _fake_restic(tmp_path, rc=0):
    """Drop a `restic` stub on PATH that exits rc; records argv to a file."""
    binp = tmp_path / "bin"
    binp.mkdir(exist_ok=True)
    log = tmp_path / "restic.log"
    (binp / "restic").write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{log}"\n'
        f"exit {rc}\n"
    )
    (binp / "restic").chmod(0o755)
    return binp, log


def test_validate_ok_when_restic_opens(rk, tmp_path, monkeypatch, capsys):
    store = _store(tmp_path)
    binp, _ = _fake_restic(tmp_path, rc=0)
    monkeypatch.setenv("PATH", f"{binp}:{os.environ['PATH']}")
    monkeypatch.setenv("CATENA_CONFIG_STORE", str(store))
    monkeypatch.setattr("sys.stdin", io.StringIO('{"op":"validate","password":"pw"}'))
    rc = rk.main()
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True}


def test_validate_fails_when_restic_rejects(rk, tmp_path, monkeypatch, capsys):
    store = _store(tmp_path)
    binp, _ = _fake_restic(tmp_path, rc=1)
    monkeypatch.setenv("PATH", f"{binp}:{os.environ['PATH']}")
    monkeypatch.setenv("CATENA_CONFIG_STORE", str(store))
    monkeypatch.setattr("sys.stdin", io.StringIO('{"op":"validate","password":"bad"}'))
    rc = rk.main()
    assert rc == 1
    assert json.loads(capsys.readouterr().out) == {"ok": False}


def test_rotate_persists_new_password(rk, tmp_path, monkeypatch, capsys):
    store = _store(tmp_path, vault_backup_restic_password="old-pw")
    passfile = tmp_path / "restic.pass"
    binp, log = _fake_restic(tmp_path, rc=0)
    monkeypatch.setenv("PATH", f"{binp}:{os.environ['PATH']}")
    monkeypatch.setenv("CATENA_CONFIG_STORE", str(store))
    monkeypatch.setenv("CATENA_RESTIC_PASS_FILE", str(passfile))
    monkeypatch.setattr("sys.stdin", io.StringIO('{"op":"rotate","new_password":"new-pw"}'))
    rc = rk.main()
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True}
    # store + pass file updated to the new password; store stays 0600.
    assert json.loads(store.read_text())["secrets"]["vault_backup_restic_password"] == "new-pw"
    assert passfile.read_text().strip() == "new-pw"
    assert stat.S_IMODE(store.stat().st_mode) == 0o600
    assert stat.S_IMODE(passfile.stat().st_mode) == 0o600
    assert "key passwd" in log.read_text()


def test_rotate_refuses_without_current_password(rk, tmp_path, monkeypatch, capsys):
    store = _store(tmp_path)  # no vault_backup_restic_password on-box
    binp, _ = _fake_restic(tmp_path, rc=0)
    monkeypatch.setenv("PATH", f"{binp}:{os.environ['PATH']}")
    monkeypatch.setenv("CATENA_CONFIG_STORE", str(store))
    monkeypatch.setattr("sys.stdin", io.StringIO('{"op":"rotate","new_password":"new-pw"}'))
    rc = rk.main()
    assert rc == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
