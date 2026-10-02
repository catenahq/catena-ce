"""Unit tests for scripts/catena-restic-key.py -- the backup-password
generate/validate/rotate helper. restic is stubbed (a fake on PATH) so no real
repo is touched; we assert the request routing, the store read, and the
persistence on generate and rotate."""
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
        "secrets": {"backup_s3_access_key": "ak",
                    "backup_s3_secret_key": "sk", **secrets},
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
    store = _store(tmp_path, backup_restic_password="old-pw")
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
    assert json.loads(store.read_text())["secrets"]["backup_restic_password"] == "new-pw"
    assert passfile.read_text().strip() == "new-pw"
    assert stat.S_IMODE(store.stat().st_mode) == 0o600
    assert stat.S_IMODE(passfile.stat().st_mode) == 0o600
    assert "key passwd" in log.read_text()


def _run(rk, monkeypatch, capsys, store, request, rc=0, tmp_path=None):
    binp, log = _fake_restic(tmp_path, rc=rc)
    monkeypatch.setenv("PATH", f"{binp}:{os.environ['PATH']}")
    monkeypatch.setenv("CATENA_CONFIG_STORE", str(store))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(request)))
    code = rk.main()
    return code, json.loads(capsys.readouterr().out), log


def test_generate_keeps_the_password_when_the_repository_is_empty(
        rk, tmp_path, monkeypatch, capsys):
    """No converge mints it: the panel's button asks, once, and the client
    saves what it shows."""
    store = _store(tmp_path)
    code, out, log = _run(rk, monkeypatch, capsys, store, {"op": "generate"},
                          rc=10, tmp_path=tmp_path)
    assert code == 0 and out["ok"] and len(out["password"]) == 64
    saved = json.loads(store.read_text())["secrets"]["backup_restic_password"]
    assert saved == out["password"]
    assert stat.S_IMODE(store.stat().st_mode) == 0o600
    assert "cat config" in log.read_text()


def test_generate_refuses_a_repository_encrypted_under_another_password(
        rk, tmp_path, monkeypatch, capsys):
    store = _store(tmp_path)
    code, out, _ = _run(rk, monkeypatch, capsys, store, {"op": "generate"},
                        rc=12, tmp_path=tmp_path)
    assert code == 1 and "Restore" in out["error"]
    assert "backup_restic_password" not in json.loads(store.read_text())["secrets"]


def test_generate_refuses_once_a_password_exists(rk, tmp_path, monkeypatch, capsys):
    store = _store(tmp_path, backup_restic_password="kept")
    code, out, _ = _run(rk, monkeypatch, capsys, store, {"op": "generate"},
                        tmp_path=tmp_path)
    assert code == 1 and "rotate" in out["error"]
    assert json.loads(store.read_text())["secrets"]["backup_restic_password"] == "kept"


def test_generate_needs_no_repository_yet(rk, tmp_path, monkeypatch, capsys):
    store = tmp_path / "config.json"
    store.write_text(json.dumps({"secrets": {}, "config": {}}))
    code, out, log = _run(rk, monkeypatch, capsys, store, {"op": "generate"},
                          rc=1, tmp_path=tmp_path)
    assert code == 0 and out["ok"]
    assert not log.exists(), "restic ran with no repository to ask"


def test_a_write_keeps_the_keys_other_writers_own(rk, tmp_path, monkeypatch, capsys):
    store = _store(tmp_path, backup_restic_password="old-pw")
    doc = json.loads(store.read_text())
    doc["schedules"] = {"backup": "weekly"}
    doc["image_pins"] = {"repo": "ref"}
    store.write_text(json.dumps(doc))
    monkeypatch.setenv("CATENA_RESTIC_PASS_FILE", str(tmp_path / "restic.pass"))
    code, _, _ = _run(rk, monkeypatch, capsys, store,
                      {"op": "rotate", "new_password": "new-pw"}, tmp_path=tmp_path)
    assert code == 0
    after = json.loads(store.read_text())
    assert after["schedules"] == {"backup": "weekly"}
    assert after["image_pins"] == {"repo": "ref"}


def test_rotate_refuses_without_current_password(rk, tmp_path, monkeypatch, capsys):
    store = _store(tmp_path)  # no backup_restic_password on-box
    binp, _ = _fake_restic(tmp_path, rc=0)
    monkeypatch.setenv("PATH", f"{binp}:{os.environ['PATH']}")
    monkeypatch.setenv("CATENA_CONFIG_STORE", str(store))
    monkeypatch.setattr("sys.stdin", io.StringIO('{"op":"rotate","new_password":"new-pw"}'))
    rc = rk.main()
    assert rc == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
