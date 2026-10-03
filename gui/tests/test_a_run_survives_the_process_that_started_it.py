"""An inventory is the run: the answers outlive the process, a password does not.

An install can start with a wait nobody can time -- a server being delivered by
a provider -- so what a client answered is saved into their inventory under
ansible/inventory/ as they go, through seed's own writer, and a launcher opened
again picks it up.

WHAT IS ON DISK AND WHAT IS NOT is the other half. The answers are; a PASSWORD
is not, anywhere in the inventory. A reopened inventory asks for it again,
which is the honest cost of refusing to write it to a client's disk.

Run: uv run pytest tests/test_a_run_survives_the_process_that_started_it.py
"""
from __future__ import annotations

import json
import os

import pytest

from catena_gui import registry, run as run_mod

DOC = registry.load()
SECRETS = run_mod.secret_keys_from(DOC)


def _files_text(path) -> str:
    return "".join(p.read_text(encoding="utf-8", errors="replace")
                   for p in path.rglob("*") if p.is_file())


def test_a_new_inventory_is_a_new_run_rather_than_an_error(tmp_path):
    r = run_mod.load(tmp_path / "clientco", SECRETS)
    assert r.state == run_mod.STATE_ANSWERING
    assert r.answers == {}
    assert r.inventory == "clientco"


def test_answers_come_back_through_the_env_and_credentials_do_not(tmp_path):
    r = run_mod.load(tmp_path / "clientco", SECRETS)
    r.answer("ADMIN_EMAIL", "admin@client.test", secret=False)
    r.answer("host_initial_password", "provider-secret", secret=True)
    r.save()

    resumed = run_mod.load(tmp_path / "clientco", SECRETS)
    assert resumed.answers["ADMIN_EMAIL"] == "admin@client.test"
    assert resumed.secrets == {}, (
        "a credential came back from disk, so it was written there")
    assert "provider-secret" not in _files_text(tmp_path / "clientco")


def test_the_env_is_the_one_the_cli_reads(tmp_path):
    """seed's reader parses what the launcher saved, so `catena-cli install
    --inventory clientco` and the launcher agree about every value."""
    r = run_mod.load(tmp_path / "clientco", SECRETS)
    r.answer("HOST_PUBLIC_IP", "198.51.100.7", secret=False)
    r.save()
    seed = run_mod.seed()
    env = seed.read_existing_env(r.env_path)
    assert env["HOST_PUBLIC_IP"] == "198.51.100.7"
    # Every key the template declares is written, the unanswered ones with the
    # template's own default.
    for key, default in seed.ENV_KEYS:
        assert key in env
        if key != "HOST_PUBLIC_IP":
            assert env[key] == default, key


def test_an_edit_replaces_the_value_already_saved(tmp_path):
    """seed keeps an existing value on a CLI re-run; the launcher's save IS
    the client's edit, so it has to land."""
    r = run_mod.load(tmp_path / "clientco", SECRETS)
    r.answer("HOST_SSH_PORT", "2222", secret=False)
    r.save()
    r.answer("HOST_SSH_PORT", "2200", secret=False)
    r.save()
    assert run_mod.load(tmp_path / "clientco", SECRETS).answers[
        "HOST_SSH_PORT"] == "2200"


def test_a_value_edited_by_hand_is_kept_by_the_launcher(tmp_path):
    """A key the launcher never asks about survives its saves."""
    r = run_mod.load(tmp_path / "clientco", SECRETS)
    r.save()
    text = r.env_path.read_text(encoding="utf-8").replace(
        "OPS_USER=ops", "OPS_USER=admin2")
    r.env_path.write_text(text, encoding="utf-8")
    reopened = run_mod.load(tmp_path / "clientco", SECRETS)
    reopened.answer("HOST_SSH_PORT", "2222", secret=False)
    reopened.save()
    assert run_mod.load(tmp_path / "clientco", SECRETS).answers[
        "OPS_USER"] == "admin2"


def test_a_reopened_inventory_says_which_credentials_it_still_needs(tmp_path):
    r = run_mod.load(tmp_path / "clientco", SECRETS)
    r.answer("host_initial_password", "pw", secret=True)
    r.save()
    resumed = run_mod.load(tmp_path / "clientco", SECRETS)
    assert resumed.missing_secrets(["host_initial_password"]) == [
        "host_initial_password"]


def test_the_writer_refuses_a_credential_a_caller_misfiled(tmp_path):
    """The second gate. The registry classifies and the caller routes; this is
    the writer enforcing the same answer."""
    r = run_mod.load(tmp_path / "clientco", SECRETS)
    r.answers["backup_s3_secret_key"] = "leaked"
    r.save()
    assert "leaked" not in _files_text(tmp_path / "clientco")


def test_the_passwords_the_install_printed_reach_no_file(tmp_path):
    r = run_mod.load(tmp_path / "clientco", SECRETS)
    r.keyset = {"admin_password": "shown-once"}
    r.save()
    assert "shown-once" not in _files_text(tmp_path / "clientco")


def test_both_files_are_0600(tmp_path):
    r = run_mod.load(tmp_path / "clientco", SECRETS)
    r.answer("HOST_PUBLIC_IP", "198.51.100.7", secret=False)
    r.save()
    for path in (r.env_path, r.state_path):
        assert (os.stat(path).st_mode & 0o777) == 0o600, path


def test_the_state_says_whether_the_install_already_started(tmp_path):
    r = run_mod.load(tmp_path / "clientco", SECRETS)
    r.state = run_mod.STATE_INSTALLING
    r.save()
    assert run_mod.load(tmp_path / "clientco", SECRETS).state == run_mod.STATE_INSTALLING


def test_a_run_whose_launcher_is_gone_reads_as_failed(tmp_path):
    """The install is a child of the launcher that started it and went with
    it, so the run can be installed again rather than staying stuck."""
    import subprocess
    import sys

    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    path = tmp_path / "clientco"
    path.mkdir()
    for doc in ({"state": "installing", "pid": gone.pid}, {"state": "installing"}):
        (path / run_mod.STATE_FILENAME).write_text(json.dumps(doc), encoding="utf-8")
        assert run_mod.load(path, SECRETS).state == run_mod.STATE_FAILED, doc


def test_a_malformed_state_file_is_an_error(tmp_path):
    path = tmp_path / "clientco"
    path.mkdir()
    (path / run_mod.STATE_FILENAME).write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError):
        run_mod.load(path, SECRETS)


def test_a_value_is_found_whichever_half_holds_it(tmp_path):
    r = run_mod.load(tmp_path / "clientco", SECRETS)
    r.answer("ADMIN_EMAIL", "admin@client.test", secret=False)
    r.answer("host_initial_password", "pw", secret=True)
    assert r.value("ADMIN_EMAIL") == "admin@client.test"
    assert r.value("host_initial_password") == "pw"
    assert r.value("never_answered") == ""


def test_the_state_file_holds_no_answer(tmp_path):
    """One home per value: the answers are the `.env`'s."""
    r = run_mod.load(tmp_path / "clientco", SECRETS)
    r.answer("ADMIN_EMAIL", "admin@client.test", secret=False)
    r.save()
    state = json.loads(r.state_path.read_text(encoding="utf-8"))
    assert "admin@client.test" not in json.dumps(state)
