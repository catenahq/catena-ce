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
RECONCILE = ANSIBLE / "playbooks" / "reconcile.yml"
# The tasks themselves, shared by both converge paths.
SHARED = ANSIBLE / "playbooks" / "tasks" / "record_release_manifest.yml"
COMMON_DEFAULTS = ANSIBLE / "bootstrap" / "roles" / "common" / "defaults" / "main.yml"

# Both playbooks that converge a host. An assertion about "the converge" has to
# hold for whichever one ran, which is the whole reason the tasks were lifted
# out of site.yml.
CONVERGE_PLAYBOOKS = (SITE, RECONCILE)


@pytest.fixture(scope="module")
def post_tasks() -> list[dict]:
    return yaml.safe_load(SHARED.read_text())


@pytest.fixture(scope="module")
def write_task(post_tasks) -> dict:
    for task in post_tasks:
        if task.get("name", "").startswith("Release: record"):
            return task
    pytest.fail(f"{SHARED.name} no longer writes the release manifest")


@pytest.mark.parametrize("playbook", CONVERGE_PLAYBOOKS, ids=lambda p: p.name)
def test_the_manifest_is_the_last_thing_the_converge_writes(playbook):
    """A manifest written mid-play describes a converge that may not finish.
    version.txt already covers "a converge started here"; this file exists to
    say "a converge finished here", and it can only say that from the end.

    Asserted for BOTH playbooks. An on-host converge that stamped the manifest
    before its last role would be making the same claim site.yml was careful
    not to."""
    play = yaml.safe_load(playbook.read_text())[0]
    names = [t.get("name", "") for t in play["post_tasks"]]
    writes = [i for i, n in enumerate(names) if n.startswith("Release: record")]
    assert writes, f"{playbook.name} no longer records the release manifest"
    assert writes[-1] == len(names) - 1, (
        f"{playbook.name} records the manifest at position {writes[-1]} of "
        f"{len(names)}; tasks after it are converge work it would be claiming "
        f"as done: {names[writes[-1] + 1:]}"
    )


@pytest.mark.parametrize("playbook", CONVERGE_PLAYBOOKS, ids=lambda p: p.name)
def test_both_converge_paths_include_the_same_file(playbook):
    """The gap this closes. These tasks lived in site.yml, so an on-host
    converge left every field describing the last converge an OPERATOR ran --
    including `actions`, which is what the panel checks before deciding a host
    needs a converge. The banner then survived the converge that cleared it."""
    play = yaml.safe_load(playbook.read_text())[0]
    includes = [t for t in play["post_tasks"]
                if "ansible.builtin.include_tasks" in t]
    files = [t["ansible.builtin.include_tasks"].get("file") for t in includes]
    assert f"tasks/{SHARED.name}" in files, (
        f"{playbook.name} does not include tasks/{SHARED.name}; it is either "
        "not stamping the manifest or keeping a second copy of these tasks")


@pytest.mark.parametrize("playbook", CONVERGE_PLAYBOOKS, ids=lambda p: p.name)
def test_each_path_names_itself_in_the_manifest(playbook):
    """validate.yml asserts this manifest's version against
    /etc/catena/version.txt, on the reasoning that one converge writes both at
    opposite ends of itself. site.yml's first role stamps version.txt;
    reconcile.yml never runs that role, so on a self-converged host the two
    describe different runs. Recording which path wrote the manifest is what
    lets a reader tell a real disagreement from drift."""
    play = yaml.safe_load(playbook.read_text())[0]
    include = next(t for t in play["post_tasks"]
                   if t.get("ansible.builtin.include_tasks", {}).get("file")
                   == f"tasks/{SHARED.name}")
    named = include.get("vars", {}).get("catena_converge_path")
    assert named, f"{playbook.name} does not name itself as the converge path"
    assert named == playbook.stem, (
        f"{playbook.name} records itself as {named!r}")
    assert "'converged_by': catena_converge_path" in SHARED.read_text()


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
    """Not a hand-kept list. reconcile/roles/catena-admin builds
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
    """The marker is what reconcile/roles/payload writes and what the self-update engine
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
    # The register name is read off the slurp rather than restated here, so a
    # rename either moves both halves or fails -- instead of passing against a
    # variable nothing sets any more.
    registered = slurp.get("register")
    assert registered, "the marker slurp registers nothing, so nothing reads it"
    assert registered in write_task["ansible.builtin.copy"]["content"]


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
    body = (ANSIBLE / "bootstrap" / "roles" / "common" / "tasks" / "validate.yml").read_text()
    assert "catena_release_manifest_path" in body
    assert "_manifest.catena_ce_version == _stamped" in body
    assert "hostvars['localhost']['catena_version']" not in body


def test_version_txt_is_untouched():
    """It is a client-facing artifact the sovereign-exit path reads straight out
    of a snapshot with `restic dump`, and the recovery README points at it. The
    manifest is additional, not a replacement."""
    body = (ANSIBLE / "bootstrap" / "roles" / "common" / "tasks" / "main.yml").read_text()
    assert "dest: /etc/catena/version.txt" in body


def test_both_version_fields_read_the_fact_this_run_actually_sets():
    """The capture is `delegate_to: localhost`, which reads the controller's git
    state but does NOT pin the fact on localhost -- the capture's own comment
    says so. Both writers used the hostvars['localhost'] form anyway, so both
    resolved to nothing and every host stamped the literal string "unknown":
    version.txt, the client-facing artifact, and the manifest's
    catena_ce_version.

    The restore version gate reads exactly this. Two "unknown" stamps compare
    Same by raw equality, so the gate passed for the wrong reason and no skew
    in either direction could ever be detected on a real install.

    They have to move together: validate.yml asserts the manifest against the
    stamp, so fixing one alone would fail every converge."""
    stamp = (ANSIBLE / "bootstrap" / "roles" / "common" / "tasks" / "main.yml").read_text()
    shared = SHARED.read_text()
    for name, body in (("bootstrap/roles/common/tasks/main.yml", stamp),
                       (SHARED.name, shared)):
        assert "hostvars['localhost']['catena_version']" not in body, (
            f"{name} is back on the hostvars form, which resolves to nothing "
            "and stamps 'unknown'"
        )
    assert "{{ catena_version | default('unknown') }}" in stamp
    assert "'catena_ce_version': _ce" in shared


def test_the_ce_version_falls_back_to_the_image_provenance():
    """An on-host converge has no git checkout and does not run the role that
    reads one, so `catena_version` is undefined there. The tree it is running
    came out of the panel image, and the vendor step recorded which catena-ce
    commit that was in VENDOR.json -- which names bytes rather than a ref, so
    it is the better answer on the path that has both."""
    shared = SHARED.read_text()
    assert "VENDOR.json" in shared, (
        "the on-host path has no other way to know which catena-ce it is "
        "applying, so this field would be blank on every host that converges "
        "itself")
    assert ".commit" in shared


def test_an_unknown_version_never_overwrites_a_known_one():
    """The field is written by whichever path ran last. A path that could not
    resolve a version must leave the previous value alone: a host converged
    from a laptop last month has a true answer in the file, and replacing it
    with a word meaning "I did not look" destroys the only record in order to
    keep the field populated."""
    shared = SHARED.read_text()
    assert "_prev.catena_ce_version" in shared, (
        "nothing carries the previous version forward, so a converge that "
        "could not resolve one blanks it")
    assert "'catena_ce_version': catena_version | default('unknown')" not in shared, (
        "'unknown' is being written over whatever the file held")


def test_the_converge_carries_the_payloads_half_forward(post_tasks, write_task):
    """Two writers own disjoint halves. The converge owns everything about the
    converge; the payload owns the actions ITS dispatch drop-in adds.

    `copy` writes full content, so the payload's half has to be read back and
    carried, exactly as the engine marker is. Without this, every converge would
    erase the record of actions the host does in fact accept, and the panel would
    raise a converge-required banner for them -- a banner that lies, on the one
    surface whose whole job is to be believed."""
    names = [t.get("name", "") for t in post_tasks]
    assert any(n.startswith("Release: read the payload") for n in names), (
        "nothing reads the previous manifest, so the converge clobbers the "
        "payload's half"
    )
    slurp = next(t for t in post_tasks
                 if t.get("name", "").startswith("Release: read the payload"))
    assert slurp["ansible.builtin.slurp"]["src"] == "{{ catena_release_manifest_path }}"
    # A first converge has no manifest to read, and a failed slurp must not
    # fail the converge at its last task.
    assert slurp.get("failed_when") is False
    content = write_task["ansible.builtin.copy"]["content"]
    assert "'payload_actions'" in content
    assert "_prev.payload_actions" in content


def test_a_corrupt_previous_manifest_does_not_fail_the_converge(write_task):
    """This file is deliberately not load-bearing -- a host with no manifest
    converges fine -- so a corrupt one must not become the thing that fails a
    converge at its very last task. from_json raises on anything that is not
    JSON, so the parse is guarded rather than trusted."""
    guard = str(write_task.get("vars", {}))
    assert "startswith('{')" in guard and "endswith('}')" in guard, (
        "from_json runs unguarded on whatever the previous file held"
    )


def test_the_slurp_runs_before_the_write(post_tasks):
    """Reading the previous manifest after overwriting it reads the file this
    task just wrote, so the carried half would be the one it just dropped."""
    names = [t.get("name", "") for t in post_tasks]
    read = next(i for i, n in enumerate(names)
                if n.startswith("Release: read the payload"))
    write = next(i for i, n in enumerate(names) if n.startswith("Release: record"))
    assert read < write, f"read at {read}, write at {write}"
