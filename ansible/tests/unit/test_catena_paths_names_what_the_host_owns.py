"""`catena-paths` answers "where does this host keep its things".

THE QUESTION IT REPLACES. Catena's files are spread across /srv, /etc,
/usr/local and /var/lib, and the obvious tidy -- put everything under one
prefix -- is wrong: the spread IS the restore boundary. /srv/catena means "this
comes back on a data restore", and /etc, /usr/local and /var/lib belong to the
machine rather than the data. Moving the config store or the payload under the
data prefix would either make a restore overwrite the new host's tunnel
credentials and machine identity with the dead host's, or leave a directory
under that prefix which restores deliberately skip.

So the answer is a listing, not a move. The host already knows what it owns, in
two manifests, and a third group -- the drop-ins in directories the operating
system owns -- can never move and is declared.

Run: uv run pytest tests/unit/test_catena_paths_names_what_the_host_owns.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
if str(ANSIBLE) not in sys.path:
    sys.path.insert(0, str(ANSIBLE))

from helpers import host_paths as hp  # noqa: E402

PREFIX = "/srv/catena"
ROLE = ANSIBLE / "reconcile" / "roles" / "public_ports"


# --- the classification is the restore boundary ------------------------------

def test_the_data_prefix_is_the_only_thing_a_restore_brings_back():
    for path in (PREFIX, f"{PREFIX}/apps",
                 f"{PREFIX}/docker/volumes/catena-postgres-data/_data"):
        assert hp.classify(path, PREFIX) == hp.TIER_DATA, path


def test_the_machine_keeps_its_own_identity():
    """These are in the snapshot and skipped by a restore. A listing that
    called them data would tell a client a rebuilt host comes back with its ssh
    host keys, its firewall state and its binaries replaced by the dead host's
    -- which is exactly what the restore engine refuses to do."""
    for path in ("/etc/catena/config.json", "/etc/ssh/ssh_host_ed25519_key",
                 "/usr/local/bin/catena-backup-run",
                 "/usr/local/lib/catena/public_ports.py",
                 "/var/lib/catena/converge-manifest.json",
                 "/var/lib/dpkg/status"):
        assert hp.classify(path, PREFIX) == hp.TIER_MACHINE, path


def test_the_os_group_wins_over_the_prefix_it_sits_in():
    """Every one of them is under /etc, which would otherwise class them as
    machine state. They are their own group because they are the half an
    operator is least likely to find, and because they can never move."""
    for path in hp.OS_MANAGED_PATHS:
        assert hp.classify(path, PREFIX) == hp.TIER_OS, path


def test_an_unknown_path_is_reported_as_machine_state():
    """The conservative answer. It says "do not expect a restore to bring this
    back", which is true of everything outside the data prefix -- and the
    opposite mistake would promise a client their data returns when it does
    not."""
    assert hp.classify("/opt/something/catena.conf", PREFIX) == hp.TIER_MACHINE


# --- reading the manifests ---------------------------------------------------

def test_both_manifest_shapes_are_read():
    """The payload manifest records an object per entry with its sha256; the
    converge's records paths. One reader, because the question -- which paths
    did you write -- is the same."""
    payload = json.dumps([{"path": "/usr/local/bin/catena-daily", "sha256": "x"}])
    converge = json.dumps(["/etc/systemd/system/catena-backup.timer"])
    assert hp.manifest_paths(payload) == ["/usr/local/bin/catena-daily"]
    assert hp.manifest_paths(converge) == [
        "/etc/systemd/system/catena-backup.timer"]


def test_a_missing_or_malformed_manifest_yields_nothing_rather_than_raising():
    """A host that has not run one of the two installers is a host with one
    manifest, not a broken one. A listing that refused to print because an
    input was absent would be least useful exactly when it is most needed."""
    assert hp.manifest_paths("") == []
    assert hp.manifest_paths("{not json") == []
    assert hp.read_manifest("/nonexistent/manifest.json") == []


def test_the_os_group_is_always_listed_even_with_no_manifests():
    """Nothing wrote it as a unit, so no manifest records it. A listing that
    only echoed the manifests would omit the group that cannot move."""
    grouped = hp.group([], PREFIX)
    assert grouped[hp.TIER_OS] == sorted(hp.OS_MANAGED_PATHS)
    assert grouped[hp.TIER_DATA] == []


def test_every_declared_path_lands_in_exactly_one_group():
    paths = [f"{PREFIX}/apps", "/etc/catena/config.json",
             "/usr/local/bin/catena-daily", *hp.OS_MANAGED_PATHS]
    grouped = hp.group(paths, PREFIX)
    seen = [p for tier in grouped.values() for p in tier]
    assert sorted(seen) == sorted(set(paths))


# --- what it prints ----------------------------------------------------------

def test_the_listing_names_every_group_and_says_what_each_means():
    """A list of paths under three headings a reader cannot interpret is a
    list of paths. The restore boundary is the reason the groups exist."""
    out = hp.render(hp.group([f"{PREFIX}/apps"], PREFIX), data_prefix=PREFIX)
    assert PREFIX in out
    # The notes are wrapped for the terminal, so compare on words rather than
    # on the unwrapped sentence.
    collapsed = " ".join(out.split())
    for tier in (hp.TIER_DATA, hp.TIER_MACHINE, hp.TIER_OS):
        assert hp.TIER_TITLES[tier] in out
        assert " ".join(hp.TIER_NOTES[tier].split()) in collapsed
    assert "restore" in out.lower()


# --- it is installed, and withdrawing it removes it --------------------------

def test_the_command_and_its_module_are_installed():
    tasks = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text())
    body = yaml.safe_dump(tasks)
    assert "host_paths.py" in body, (
        "the classifier is not installed, so the command would import nothing")
    assert "catena-paths.py" in body, "the command itself is not installed"


def test_it_is_declared_so_a_later_release_can_withdraw_it():
    """Undeclared means unmanaged, never deleted. A script left on a host after
    the product stops shipping it is the one that gets imported, so the prune
    leg has to know about both files."""
    defaults = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
    managed = defaults["public_ports_managed_paths"]
    assert "{{ public_ports_lib_dir }}/host_paths.py" in managed
    assert "{{ catena_paths_bin }}" in managed
    assert defaults["catena_paths_bin"].startswith("/usr/local/bin/"), (
        "the command is not on PATH, which is what makes it findable -- the "
        "whole point of a command that answers where things are")
