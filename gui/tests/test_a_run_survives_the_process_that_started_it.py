"""An install contains two waits nobody can time, so the answers outlive the run.

A server being delivered by a provider, and a domain being activated by its
registrar. Neither fits inside a page load or a sitting somebody is present
for, which is why the run directory is the single biggest structural
requirement of the launcher -- above any individual step.

WHAT IS ON DISK AND WHAT IS NOT is the other half. The answers are; the
CREDENTIALS are not. A resumed run asks for them again, which is the honest
cost of refusing to write a client's cloud credentials to their disk: the
alternative is a file that outlives the install and that nothing ever comes
back to remove.

Run: uv run pytest tests/test_a_run_survives_the_process_that_started_it.py
"""
from __future__ import annotations

import json
import os

import pytest

from catena_gui import registry, run as run_mod

SECRETS = run_mod.secret_keys_from(registry.load())


def test_a_new_directory_is_a_new_run_rather_than_an_error(tmp_path):
    """The launcher is started with a path and makes it, which is the same
    shape as being started twice with the same path."""
    r = run_mod.load(tmp_path / "never-used", SECRETS)
    assert r.state == run_mod.STATE_ANSWERING
    assert r.answers == {}


def test_answers_come_back_and_credentials_do_not(tmp_path):
    r = run_mod.load(tmp_path / "run", SECRETS)
    r.inventory = "clientco"
    r.answer("CLOUDFLARE_ZONE", "client.test", secret=False)
    r.answer("cloudflare_api_token", "cf-secret", secret=True)
    r.save()

    resumed = run_mod.load(tmp_path / "run", SECRETS)
    assert resumed.answers["CLOUDFLARE_ZONE"] == "client.test"
    assert resumed.inventory == "clientco"
    assert resumed.secrets == {}, (
        "a credential came back from disk, so it was written there")
    assert "cf-secret" not in resumed.answers_path.read_text(encoding="utf-8")


def test_a_resumed_run_says_which_credentials_it_still_needs(tmp_path):
    """Non-empty on every resumed run that needs one, which is the point: the
    launcher asks again rather than pretending a value it deliberately did not
    keep is still available."""
    r = run_mod.load(tmp_path / "run", SECRETS)
    r.answer("cloudflare_api_token", "cf", secret=True)
    r.save()
    resumed = run_mod.load(tmp_path / "run", SECRETS)
    assert resumed.missing_secrets(["cloudflare_api_token"]) == [
        "cloudflare_api_token"]


def test_the_writer_refuses_a_credential_a_caller_misfiled(tmp_path):
    """The second gate. The registry classifies and the caller routes; this is
    the writer enforcing the same answer, so a caller that assigned the whole
    form into `answers` could not persist a token by forgetting to filter."""
    r = run_mod.load(tmp_path / "run", SECRETS)
    r.answers["backup_s3_secret_key"] = "leaked"
    r.save()
    on_disk = json.loads(r.answers_path.read_text(encoding="utf-8"))
    assert "backup_s3_secret_key" not in on_disk["answers"]
    assert "leaked" not in r.answers_path.read_text(encoding="utf-8")


def test_a_path_that_merely_contains_the_word_key_is_persisted(tmp_path):
    """The reason the classification is the registry's rather than the name's.
    SSH_PRIVATE_KEY holds a path and _keyset_acknowledged holds a yes; a name
    heuristic filed both as credentials, and the launcher could then not read
    back its own answers on a resumed run."""
    r = run_mod.load(tmp_path / "run", SECRETS)
    r.answer("SSH_PRIVATE_KEY", "~/.ssh/catena_ed25519", secret=False)
    r.answer("_keyset_acknowledged", "yes", secret=False)
    r.save()
    resumed = run_mod.load(tmp_path / "run", SECRETS)
    assert resumed.answers["SSH_PRIVATE_KEY"] == "~/.ssh/catena_ed25519"
    assert resumed.answers["_keyset_acknowledged"] == "yes"


def test_the_answers_file_is_0600(tmp_path):
    """Which server and which domain is still a client's business, even though
    neither opens anything."""
    r = run_mod.load(tmp_path / "run", SECRETS)
    r.answer("HOST_PUBLIC_IP", "203.0.113.10", secret=False)
    r.save()
    assert (os.stat(r.answers_path).st_mode & 0o777) == 0o600


def test_the_state_says_whether_the_install_already_started(tmp_path):
    """What a resumed run needs to know. A launcher that reopened the form for
    a run whose install is still going would let a client answer the same
    questions twice into a host that is already being built."""
    r = run_mod.load(tmp_path / "run", SECRETS)
    r.state = run_mod.STATE_INSTALLING
    r.step = "keyset"
    r.save()
    resumed = run_mod.load(tmp_path / "run", SECRETS)
    assert resumed.state == run_mod.STATE_INSTALLING
    assert resumed.step == "keyset"


def test_a_malformed_answers_file_is_an_error(tmp_path):
    """Resuming from one would silently drop whichever answers failed to
    parse, and a client would meet them again as blank fields with no
    explanation."""
    path = tmp_path / "run"
    path.mkdir()
    (path / run_mod.ANSWERS_FILENAME).write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError):
        run_mod.load(path, SECRETS)


def test_a_value_is_found_whichever_half_holds_it(tmp_path):
    """A page rendering a field does not care which half of the run the value
    came from. Only the writer does."""
    r = run_mod.load(tmp_path / "run", SECRETS)
    r.answer("CLOUDFLARE_ZONE", "client.test", secret=False)
    r.answer("cloudflare_api_token", "cf", secret=True)
    assert r.value("CLOUDFLARE_ZONE") == "client.test"
    assert r.value("cloudflare_api_token") == "cf"
    assert r.value("never_answered") == ""
