"""The release manifest the converge writes at the end of site.yml.

It is the panel's only way to tell "this feature is off" from "this feature's
plumbing never reached this host". Every button dispatches a FIXED action name
through the SSH forced command, and that table is rendered by the converge, so
a panel built after an action was added shows a working-looking button on a host
whose converge predates it.

Three properties are worth pinning, and each of them has already been the bug
somewhere else in this repo:

  - it is written LAST, so a converge that died at role 9 leaves the previous
    manifest standing instead of claiming plumbing it never delivered;
  - the action list comes from the same merged fact the dispatch table renders
    from, not a hand-kept copy that drifts;
  - the image field goes through the pin filter, or the manifest reports the
    catalog floor on a host the update lane moved.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
SITE = ANSIBLE / "playbooks" / "site.yml"
COMMON_DEFAULTS = ANSIBLE / "roles" / "common" / "defaults" / "main.yml"


@pytest.fixture(scope="module")
def post_tasks() -> list[dict]:
    play = yaml.safe_load(SITE.read_text())[0]
    return play["post_tasks"]


@pytest.fixture(scope="module")
def write_task(post_tasks) -> dict:
    for task in post_tasks:
        if task.get("name", "").startswith("Release: record"):
            return task
    pytest.fail("site.yml no longer writes the release manifest")


def test_the_manifest_is_the_last_thing_the_converge_writes(post_tasks):
    """A manifest written mid-play describes a converge that may not finish.
    version.txt already covers "a converge started here"; this file exists to
    say "a converge finished here", and it can only say that from the end."""
    names = [t.get("name", "") for t in post_tasks]
    writes = [i for i, n in enumerate(names) if n.startswith("Release: record")]
    assert writes, "site.yml no longer writes the release manifest"
    assert writes[-1] == len(names) - 1, (
        f"the manifest is written at position {writes[-1]} of {len(names)}; "
        f"tasks after it are converge work it would be claiming as done: "
        f"{names[writes[-1] + 1:]}"
    )


def test_the_write_does_not_report_a_change_every_converge(write_task):
    """`converged_at` moves on every run, so `copy` would report changed every
    time. A converge that always reports a change is one nobody can read for
    real drift -- and it fails the whole-site idempotency rehearsal
    (ce_converge), which is how this was caught. /etc/catena/version.txt has
    carried the same suppression, for the same reason, since it was written."""
    assert write_task.get("changed_when") is False, (
        "the manifest write reports changed on every converge"
    )


def test_the_action_list_comes_from_the_dispatch_fact(write_task):
    """Not a hand-kept list. roles/catena-admin builds
    _catena_admin_actions_dispatch by merging the canonical catalog with the
    reserved names, and renders the host's allow-list and dispatch table from
    it -- so reading anything else here is a copy that drifts the first time
    somebody adds a reserved action."""
    content = write_task["ansible.builtin.copy"]["content"]
    assert "_catena_admin_actions_dispatch" in content, content
    assert "map(attribute='name')" in content, content
    # A role skipped by tags leaves the fact undefined, and a converge that
    # fails on an undefined variable at its very last task is a converge that
    # threw away everything it did.
    assert "default([])" in content, content


def test_the_recorded_image_is_the_pin_resolved_one(write_task):
    """catena_admin_image already resolves max(floor, pin). Recording the floor
    instead would report the shipped version on a host the on-host update lane
    moved -- which is the same class of defect as the converge asserting it."""
    content = write_task["ansible.builtin.copy"]["content"]
    assert "catena_admin_image" in content
    assert "catena_admin_image_floor" not in content, (
        "the manifest records the floor rather than what the host runs"
    )


def test_the_payload_id_is_read_from_the_marker_not_guessed(post_tasks, write_task):
    """The marker is what roles/payload writes and what the self-update engine
    rewrites, so it is the one record of which image the installed engines came
    from. It has to be READ here: an out-of-band install moves the marker and
    nothing else."""
    names = [t.get("name", "") for t in post_tasks]
    assert any(n.startswith("Release: read the marker") for n in names)
    slurp = next(t for t in post_tasks if t.get("name", "").startswith("Release: read the marker"))
    assert slurp["ansible.builtin.slurp"]["src"] == "{{ catena_payload_marker }}"
    # A host that has never installed the payload has no marker, and a failed
    # slurp must not fail the converge at its last task.
    assert slurp.get("failed_when") is False
    assert "_site_payload_marker" in write_task["ansible.builtin.copy"]["content"]


def test_the_manifest_path_is_under_var_lib_not_etc():
    """Three reasons, and the third is the one that bites: the panel mounts
    /var/lib/catena read-only already and mounts /etc/catena file by file, so
    an /etc path would need a new mount of a file that does not exist on a first
    converge -- which docker creates as a DIRECTORY. It is also rebuildable
    state rather than configuration, and it must not ride a restic snapshot: a
    restored host's converge state is its own, not the source's."""
    defaults = yaml.safe_load(COMMON_DEFAULTS.read_text())
    path = defaults["catena_release_manifest_path"]
    assert path == "/var/lib/catena/release.json", path


def test_validate_asserts_the_manifest_against_the_version_stamp():
    """The two are written by the same converge at opposite ends of it, so
    equality is what says the converge reached the end. Comparing against the
    controller's git describe would not work: validate.yml is its own playbook
    run and never sets that fact, so the comparison would be against
    'unknown' on every host."""
    body = (ANSIBLE / "roles" / "common" / "tasks" / "validate.yml").read_text()
    assert "catena_release_manifest_path" in body
    assert "_manifest.catena_ce_version == _stamped" in body
    assert "hostvars['localhost']['catena_version']" not in body


def test_version_txt_is_untouched():
    """It is a client-facing artifact the sovereign-exit path reads straight out
    of a snapshot with `restic dump`, and the recovery README points at it. The
    manifest is additional, not a replacement."""
    body = (ANSIBLE / "roles" / "common" / "tasks" / "main.yml").read_text()
    assert "dest: /etc/catena/version.txt" in body
