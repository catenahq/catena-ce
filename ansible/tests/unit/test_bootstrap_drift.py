"""The converge reports changes to the files it may never write.

boundary.yml's `bootstrap_owned_paths` is the invariant "a reconcile may not
modify anything that would remove your ability to run a reconcile", as a list
of literals. Right rule, and it left those six paths with no reader at all: a
change to the forced command, the sudoers drop-in or the SSH trust path was
invisible on the host until an operator went looking, and what makes an
operator go looking is already suspecting something.

Not writing is not the same as not noticing. This is the noticing half, and
these are the properties that keep it honest:

  - it reads the path list from boundary.yml rather than restating it, so a
    path added there is covered without a second edit;
  - it reports and never repairs, because repair is bootstrap's;
  - it distinguishes added/removed/changed, because a removed sudoers drop-in
    and an added one are not the same event;
  - a first converge records a baseline and claims no drift, because the
    baseline is what the host has, not what it should have.

Run: uv run pytest tests/unit/test_bootstrap_drift.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

_ANSIBLE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ANSIBLE / "playbooks" / "filter_plugins"))

from bootstrap_drift import bootstrap_digests, bootstrap_drift  # noqa: E402

_TASKS = _ANSIBLE / "playbooks" / "tasks" / "report_bootstrap_drift.yml"


def _find(path, checksum):
    return {"files": [{"path": path, "checksum": checksum}]}


def _stat(path, checksum, exists=True):
    return {"stat": {"path": path, "checksum": checksum, "exists": exists}}


# --- the comparison --------------------------------------------------------
def test_nothing_moved_reports_nothing():
    now = {"/etc/sudoers.d/catena": "aa"}
    assert bootstrap_drift(now, now) == {"added": [], "removed": [], "changed": []}


def test_a_rewritten_file_is_changed():
    drift = bootstrap_drift({"/usr/local/sbin/catena-admin-runner": "aa"},
                            {"/usr/local/sbin/catena-admin-runner": "bb"})
    assert drift["changed"] == ["/usr/local/sbin/catena-admin-runner"]
    assert drift["added"] == [] and drift["removed"] == []


def test_added_and_removed_are_not_merged():
    """A removed sudoers drop-in and an added one are different events, and a
    report that called them "2 changes" would be one an operator has to
    re-derive before it is worth anything."""
    drift = bootstrap_drift({"/etc/sudoers.d/old": "aa"},
                            {"/etc/sudoers.d/new": "bb"})
    assert drift["added"] == ["/etc/sudoers.d/new"]
    assert drift["removed"] == ["/etc/sudoers.d/old"]
    assert drift["changed"] == []


def test_a_first_converge_has_no_baseline_and_claims_no_drift():
    """The baseline is what the last converge saw. A first one records and
    reports nothing -- claiming drift against an empty record would flag every
    file on every new host."""
    assert bootstrap_drift({}, {"/etc/ssh/sshd_config": "aa"}) == {
        "added": ["/etc/ssh/sshd_config"], "removed": [], "changed": []}
    assert bootstrap_drift(None, None) == {"added": [], "removed": [], "changed": []}


def test_an_unreadable_digest_is_not_reported_as_a_change():
    """An empty digest means the file was recorded but not read. Reporting that
    as drift every converge would be noise about the reader rather than about
    the file, and noise is how a report gets ignored."""
    assert bootstrap_drift({"/etc/ssh/x": ""}, {"/etc/ssh/x": "aa"})["changed"] == []
    assert bootstrap_drift({"/etc/ssh/x": "aa"}, {"/etc/ssh/x": ""})["changed"] == []


# --- building the picture --------------------------------------------------
def test_both_find_and_stat_results_are_read():
    """The directories come back from `find` as a files list and the named
    files from `stat` as one dict each. A reader that handled only one shape
    would silently cover half the list."""
    got = bootstrap_digests([_find("/etc/sudoers.d/catena", "aa")],
                            [_stat("/usr/local/sbin/catena-admin-runner", "bb")])
    assert got == {"/etc/sudoers.d/catena": "aa",
                   "/usr/local/sbin/catena-admin-runner": "bb"}


def test_a_file_that_is_not_there_is_not_recorded():
    assert bootstrap_digests([], [_stat("/etc/gone", "", exists=False)]) == {}


# --- the wiring ------------------------------------------------------------
def test_the_path_list_is_read_from_the_boundary_not_restated():
    """boundary.yml is the declaration. A copy here would be a second thing to
    keep true, and the failure mode is the copy going stale while the report
    quietly stops covering whatever was added to the real list."""
    body = _TASKS.read_text(encoding="utf-8")
    assert "boundary.yml" in body and "bootstrap_owned_paths" in body
    # Comments and task names stripped: both are prose, and prose naming a
    # path explains the handling it needs. What must not exist is a path the
    # tasks ACT on without having read it from the declaration.
    code = "\n".join(line for line in body.splitlines()
                     if not line.lstrip().startswith(("#", "- name:", "name:")))
    declared = yaml.safe_load(
        (_ANSIBLE / "boundary.yml").read_text(encoding="utf-8"))
    assert declared["bootstrap_owned_paths"], "the boundary declares no owned paths"
    for path in declared["bootstrap_owned_paths"]:
        assert path not in code, (
            f"{path!r} is restated in {_TASKS.name}; it is supposed to come "
            "from boundary.yml at run time")


def test_the_report_never_writes_a_bootstrap_owned_path():
    """The whole point. A reconcile that repaired these would be a reconcile
    that can rewrite what a reconcile is -- which is the invariant the boundary
    exists to hold, and this file runs on the reconcile side."""
    tasks = yaml.safe_load(_TASKS.read_text(encoding="utf-8"))
    writers = {"ansible.builtin.copy", "ansible.builtin.template",
               "ansible.builtin.file", "ansible.builtin.lineinfile",
               "ansible.builtin.blockinfile", "ansible.builtin.replace"}
    drift_record = None
    for task in tasks:
        for module in writers & set(task):
            dest = (task[module].get("dest") or task[module].get("path") or "")
            assert "{{ catena_bootstrap_drift_path }}" in str(dest), (
                f"{task.get('name')!r} writes {dest!r}; the only thing this "
                "file may write is its own record")
            drift_record = dest
    assert drift_record, "the report records nothing, so there is no baseline"


def test_both_converge_paths_report():
    """A report that ran on only one path would leave the host that converges
    itself -- the unattended one, where nobody is watching -- as the one with
    no reader."""
    for name in ("site.yml", "reconcile.yml"):
        play = yaml.safe_load((_ANSIBLE / "playbooks" / name).read_text())[0]
        files = [t.get("ansible.builtin.include_tasks", {}).get("file")
                 for t in play["post_tasks"]]
        assert "tasks/report_bootstrap_drift.yml" in files, name


def test_the_record_is_not_in_etc():
    """A baseline restored from a snapshot was taken on a different machine,
    which for a drift report is worse than none: every file reads as changed."""
    shared = yaml.safe_load(
        (_ANSIBLE / "playbooks" / "group_vars" / "all" / "main.yml")
        .read_text(encoding="utf-8"))
    assert shared["catena_bootstrap_drift_path"].startswith("/var/lib/")
