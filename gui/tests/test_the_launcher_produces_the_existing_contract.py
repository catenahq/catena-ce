"""What the launcher hands the installer is the file a person would have written.

NO NEW MIDDLE LAYER. `install.yaml` plus `catena install -i ... --no-confirm`
is already the declarative, non-interactive contract, and seed already
live-probes what it is given. The launcher is a THIRD producer of that file,
beside a person writing it and the test bench rendering it.

That is what makes the acceptance test possible at all: a host the launcher
built is indistinguishable from one `catena install -i install.yaml` built,
because it IS one.

Run: uv run pytest tests/test_the_launcher_produces_the_existing_contract.py
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml

from catena_gui import render


def _rendered(**kw) -> dict:
    return yaml.safe_load(render.install_yaml(**kw))


def test_the_file_is_flat_the_way_seed_splits_it():
    """FLAT, not nested. seed splits a flat file by asking the registry which
    names are secrets rather than by reading a prefix -- the same question the
    launcher asked to decide what not to write to disk. One classification, so
    a credential cannot be filed as config by either."""
    doc = _rendered(
        inventory="clientco",
        answers={"CLOUDFLARE_ZONE": "client.test", "HOST_PUBLIC_IP": "203.0.113.10"},
        secrets={"cloudflare_api_token": "cf"})
    assert doc["inventory"] == "clientco"
    assert doc["CLOUDFLARE_ZONE"] == "client.test"
    assert doc["cloudflare_api_token"] == "cf"
    assert "env" not in doc and "vault" not in doc


def test_the_launchers_own_bookkeeping_does_not_reach_the_installer():
    """An acknowledgement and a page position describe the RUN, not the
    install. seed files an unknown key as .env config, which is how a wizard's
    bookkeeping ends up in a client's inventory."""
    doc = _rendered(
        inventory="clientco",
        answers={"_keyset_acknowledged": "yes", "_step": "keyset",
                 "COMMON_LOCALE": "en_CA.UTF-8"},
        secrets={})
    assert "_keyset_acknowledged" not in doc
    assert "_step" not in doc
    assert doc["COMMON_LOCALE"] == "en_CA.UTF-8"


def test_a_blank_answer_is_left_out_rather_than_written_empty():
    """A blank is an answer for several knobs -- a server installed before its
    domain is decided is the product's own case -- and writing the key with an
    empty value makes seed's structural check treat it as supplied."""
    doc = _rendered(inventory="c", answers={"CLOUDFLARE_ZONE": "  "}, secrets={})
    assert "CLOUDFLARE_ZONE" not in doc


def test_the_host_name_rides_only_when_it_was_chosen():
    """The bench adds a distinctly-named host per run to one inventory. A
    launcher run has one server and lets seed name it."""
    assert "host_name" not in _rendered(inventory="c", answers={}, secrets={})
    doc = _rendered(inventory="c", answers={}, secrets={}, host_name="clientco1")
    assert doc["host_name"] == "clientco1"


def test_the_file_is_0600_from_creation(tmp_path: Path):
    """A chmod AFTER the write leaves a window in which the file holding a
    client's Cloudflare token and their S3 keys is world-readable, and on a
    shared machine that window is the whole exposure."""
    target = tmp_path / "install.yaml"
    render.write_install_yaml(target, "inventory: c\n")
    assert (os.stat(target).st_mode & 0o777) == 0o600


def test_the_command_is_the_cli_the_bench_already_drives(tmp_path: Path):
    """--no-confirm because the confirmation already happened: a client walked
    six pages and pressed the button. A second prompt, on a process whose
    console they may have closed, would stop the install and look like a hang."""
    argv = render.install_command(tmp_path / "ansible",
                                  tmp_path / "install.yaml", "clientco")
    assert argv[:2] == ["uv", "run"]
    assert "catena" in argv and "install" in argv
    assert "--no-confirm" in argv
    assert argv[argv.index("--inventory") + 1] == "clientco"
    assert argv[argv.index("-i") + 1] == str(tmp_path / "install.yaml")
