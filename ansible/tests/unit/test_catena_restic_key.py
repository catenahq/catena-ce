"""Unit tests for scripts/catena-restic-key.py -- the backup-password
generate/validate/rotate helper and the repository init. restic and rclone are
stubbed (fakes on PATH) so no real repo is touched; we assert the request
routing, the store read, the persistence on generate and rotate, and when init
creates a repository."""
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


def _fake_tools(tmp_path, cat_rc, lsf_out="", lsf_rc=0):
    """`restic` answering `cat config` with cat_rc and everything else with 0,
    and `rclone` answering `lsf` with lsf_out and lsf_rc. Each logs its argv,
    rclone with the endpoint its remote was given."""
    binp = tmp_path / "bin"
    binp.mkdir(exist_ok=True)
    restic_log, rclone_log = tmp_path / "restic.log", tmp_path / "rclone.log"
    (binp / "restic").write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{restic_log}"\n'
        f'[ "$1 $2" = "cat config" ] && exit {cat_rc}\n'
        "exit 0\n"
    )
    (binp / "rclone").write_text(
        "#!/bin/sh\n"
        f'printf "%s %s\\n" "$RCLONE_CONFIG_DEST_ENDPOINT" "$*" >> "{rclone_log}"\n'
        f"printf '%s' '{lsf_out}'\n"
        f"exit {lsf_rc}\n"
    )
    for tool in ("restic", "rclone"):
        (binp / tool).chmod(0o755)
    return binp, restic_log, rclone_log


def _init(rk, monkeypatch, capsys, store, binp):
    monkeypatch.setenv("PATH", f"{binp}:{os.environ['PATH']}")
    monkeypatch.setenv("CATENA_CONFIG_STORE", str(store))
    monkeypatch.setattr("sys.stdin", io.StringIO('{"op":"init"}'))
    code = rk.main()
    return code, json.loads(capsys.readouterr().out)


def _calls(log):
    return log.read_text().splitlines() if log.exists() else []


def test_init_creates_the_repository_where_the_destination_is_empty(
        rk, tmp_path, monkeypatch, capsys):
    """Saving the destination is what creates its repository, with the stored
    password; the location is listed first, as an rclone remote on the same
    endpoint and keys."""
    store = _store(tmp_path, backup_restic_password="pw")
    binp, restic_log, rclone_log = _fake_tools(tmp_path, cat_rc=10)
    code, out = _init(rk, monkeypatch, capsys, store, binp)
    assert code == 0 and out == {"ok": True, "repository": "created"}
    assert _calls(restic_log) == ["cat config --no-lock", "init"]
    assert _calls(rclone_log) == ["https://ep lsf dest:bucket"]


def test_init_lists_the_prefix_on_the_endpoint_the_repository_names(
        rk, tmp_path, monkeypatch, capsys):
    store = _store(tmp_path, backup_restic_password="pw")
    doc = json.loads(store.read_text())
    doc["config"]["BACKUP_RESTIC_REPO"] = "s3:http://10.0.0.9:9000/bucket/host-a"
    store.write_text(json.dumps(doc))
    binp, _, rclone_log = _fake_tools(tmp_path, cat_rc=10)
    code, out = _init(rk, monkeypatch, capsys, store, binp)
    assert code == 0 and out["repository"] == "created"
    assert _calls(rclone_log) == ["http://10.0.0.9:9000 lsf dest:bucket/host-a"]


def test_init_creates_the_repository_in_a_bucket_that_does_not_exist_yet(
        rk, tmp_path, monkeypatch, capsys):
    store = _store(tmp_path, backup_restic_password="pw")
    binp, restic_log, _ = _fake_tools(tmp_path, cat_rc=10, lsf_rc=3)
    code, out = _init(rk, monkeypatch, capsys, store, binp)
    assert code == 0 and out["repository"] == "created"
    assert "init" in _calls(restic_log)


def test_init_refuses_a_destination_holding_other_files(rk, tmp_path, monkeypatch, capsys):
    store = _store(tmp_path, backup_restic_password="pw")
    binp, restic_log, _ = _fake_tools(tmp_path, cat_rc=10, lsf_out="urn:oid:42")
    code, out = _init(rk, monkeypatch, capsys, store, binp)
    assert code == 1 and "not a restic repository" in out["error"]
    assert "init" not in _calls(restic_log)


def test_init_refuses_a_destination_it_cannot_list(rk, tmp_path, monkeypatch, capsys):
    store = _store(tmp_path, backup_restic_password="pw")
    binp, restic_log, _ = _fake_tools(tmp_path, cat_rc=10, lsf_rc=1)
    code, out = _init(rk, monkeypatch, capsys, store, binp)
    assert code == 1 and "could not list" in out["error"]
    assert "init" not in _calls(restic_log)


def test_init_leaves_a_repository_the_password_opens_alone(
        rk, tmp_path, monkeypatch, capsys):
    """A re-save of the destination: the repository is there, and stays as it
    is."""
    store = _store(tmp_path, backup_restic_password="pw")
    binp, restic_log, rclone_log = _fake_tools(tmp_path, cat_rc=0)
    code, out = _init(rk, monkeypatch, capsys, store, binp)
    assert code == 0 and out == {"ok": True, "repository": "exists"}
    assert _calls(restic_log) == ["cat config --no-lock"]
    assert not rclone_log.exists()


def test_init_refuses_a_repository_under_another_password(
        rk, tmp_path, monkeypatch, capsys):
    store = _store(tmp_path, backup_restic_password="pw")
    binp, restic_log, _ = _fake_tools(tmp_path, cat_rc=12)
    code, out = _init(rk, monkeypatch, capsys, store, binp)
    assert code == 1 and "Restore" in out["error"]
    assert _calls(restic_log) == ["cat config --no-lock"]


def test_init_needs_the_password(rk, tmp_path, monkeypatch, capsys):
    store = _store(tmp_path)
    binp, restic_log, _ = _fake_tools(tmp_path, cat_rc=10)
    code, out = _init(rk, monkeypatch, capsys, store, binp)
    assert code == 1 and "generate" in out["error"]
    assert not restic_log.exists(), "restic ran with no password to open a repository with"


def _hold(tmp_path, monkeypatch, settings="s3:ep/bucket"):
    """The record that the backups wait after a restore whose adopted store
    named `settings`."""
    held = tmp_path / "backup-held.json"
    held.write_text(json.dumps({"at": "2026-10-10T14:00:00Z", "settings": settings}))
    monkeypatch.setenv("CATENA_BACKUP_HELD_FILE", str(held))
    return held


def test_rotate_refuses_while_the_backups_wait(rk, tmp_path, monkeypatch, capsys):
    """The repository a restore brought back is written to only once an
    administrator confirmed it: a re-key is a write."""
    store = _store(tmp_path, backup_restic_password="old-pw")
    _hold(tmp_path, monkeypatch)
    monkeypatch.setenv("CATENA_RESTIC_PASS_FILE", str(tmp_path / "restic.pass"))
    code, out, log = _run(rk, monkeypatch, capsys, store,
                          {"op": "rotate", "new_password": "new-pw"}, tmp_path=tmp_path)
    assert code == 1 and "wait" in out["error"]
    assert not log.exists(), "restic ran while the backups wait"
    assert json.loads(store.read_text())["secrets"]["backup_restic_password"] == "old-pw"


def test_a_new_destination_saved_while_held_gets_a_new_password(
        rk, tmp_path, monkeypatch, capsys):
    """After a restore the old password may be known to whoever compromised the
    server; a new destination is created under a new one, shown once."""
    store = _store(tmp_path, backup_restic_password="old-pw")
    doc = json.loads(store.read_text())
    doc["config"]["BACKUP_RESTIC_REPO"] = "s3:https://ep/new-bucket"
    store.write_text(json.dumps(doc))
    _hold(tmp_path, monkeypatch, settings="s3:ep/bucket")
    passfile = tmp_path / "restic.pass"
    monkeypatch.setenv("CATENA_RESTIC_PASS_FILE", str(passfile))
    code, out, log = _run(rk, monkeypatch, capsys, store,
                          {"op": "generate", "replace": True}, rc=10, tmp_path=tmp_path)
    assert code == 0 and out["ok"] and out["password"] != "old-pw"
    assert json.loads(store.read_text())["secrets"]["backup_restic_password"] == out["password"]
    assert passfile.read_text().strip() == out["password"]
    assert "cat config" in log.read_text()


def test_a_password_is_replaced_only_for_a_new_empty_destination_while_held(
        rk, tmp_path, monkeypatch, capsys):
    for name, held, repo, rc, words in (
        ("no hold", False, "s3:ep/new-bucket", 10, "rotate"),
        # The same bucket, spelled with the scheme restic assumes.
        ("the adopted destination", True, "s3:https://ep/bucket", 10, "new destination"),
        ("a destination holding a repository", True, "s3:ep/new-bucket", 12, "Restore"),
    ):
        store = _store(tmp_path, backup_restic_password="old-pw")
        doc = json.loads(store.read_text())
        doc["config"]["BACKUP_RESTIC_REPO"] = repo
        store.write_text(json.dumps(doc))
        if held:
            _hold(tmp_path, monkeypatch)
        else:
            monkeypatch.setenv("CATENA_BACKUP_HELD_FILE", str(tmp_path / "absent.json"))
        monkeypatch.setenv("CATENA_RESTIC_PASS_FILE", str(tmp_path / "restic.pass"))
        code, out, _ = _run(rk, monkeypatch, capsys, store,
                            {"op": "generate", "replace": True}, rc=rc, tmp_path=tmp_path)
        assert code == 1 and words in out["error"], name
        assert json.loads(store.read_text())["secrets"]["backup_restic_password"] == "old-pw", name


def test_the_adopted_destination_gets_a_new_password_only_on_a_fresh_choice(
        rk, tmp_path, monkeypatch, capsys):
    """Confirming the destination the restore brought back re-keys it when the
    Restore page's choice is fresh, and leaves the password alone otherwise."""
    for choice, minted in (("fresh", True), ("continuity", False), (None, False)):
        store = _store(tmp_path, backup_restic_password="old-pw")
        held = tmp_path / "backup-held.json"
        hold = {"at": "2026-10-10T14:00:00Z", "settings": "s3:ep/bucket"}
        if choice:
            hold["choice"] = choice
        held.write_text(json.dumps(hold))
        monkeypatch.setenv("CATENA_BACKUP_HELD_FILE", str(held))
        passfile = tmp_path / "restic.pass"
        monkeypatch.setenv("CATENA_RESTIC_PASS_FILE", str(passfile))
        code, out, log = _run(rk, monkeypatch, capsys, store,
                              {"op": "generate", "replace": True}, rc=0, tmp_path=tmp_path)
        saved = json.loads(store.read_text())["secrets"]["backup_restic_password"]
        if minted:
            assert code == 0 and out["ok"] and out["password"] != "old-pw", choice
            assert saved == out["password"] and passfile.read_text().strip() == saved, choice
            assert "key passwd" in log.read_text(), choice
        else:
            assert code == 1 and "new destination" in out["error"], choice
            assert saved == "old-pw", choice
            assert not log.exists(), choice
        if log.exists():
            log.unlink()


def test_a_fresh_re_key_that_restic_refuses_keeps_the_stored_password(
        rk, tmp_path, monkeypatch, capsys):
    store = _store(tmp_path, backup_restic_password="old-pw")
    held = tmp_path / "backup-held.json"
    held.write_text(json.dumps({"settings": "s3:ep/bucket", "choice": "fresh"}))
    monkeypatch.setenv("CATENA_BACKUP_HELD_FILE", str(held))
    monkeypatch.setenv("CATENA_RESTIC_PASS_FILE", str(tmp_path / "restic.pass"))
    code, out, _ = _run(rk, monkeypatch, capsys, store,
                        {"op": "generate", "replace": True}, rc=1, tmp_path=tmp_path)
    assert code == 1 and "key passwd failed" in out["error"]
    assert json.loads(store.read_text())["secrets"]["backup_restic_password"] == "old-pw"


def test_rotate_refuses_without_current_password(rk, tmp_path, monkeypatch, capsys):
    store = _store(tmp_path)  # no backup_restic_password on-box
    binp, _ = _fake_restic(tmp_path, rc=0)
    monkeypatch.setenv("PATH", f"{binp}:{os.environ['PATH']}")
    monkeypatch.setenv("CATENA_CONFIG_STORE", str(store))
    monkeypatch.setattr("sys.stdin", io.StringIO('{"op":"rotate","new_password":"new-pw"}'))
    rc = rk.main()
    assert rc == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
