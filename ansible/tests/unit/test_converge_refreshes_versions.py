"""Both converge paths rewrite the version report after their roles, so the
panel's System and Apps pages name what an update left running."""
from __future__ import annotations

import pytest
import yaml

from ansible_tree import PLAYBOOKS, post_tasks

_REFRESH = "Versions: refresh the version report"


@pytest.mark.parametrize("playbook", ["converge.yml", "reconcile.yml"])
def test_the_converge_refreshes_the_version_report_before_the_manifest(playbook):
    tasks = post_tasks(PLAYBOOKS / playbook)
    names = [t.get("name", "") for t in tasks]
    assert _REFRESH in names, f"{playbook} leaves the version report to its timer"
    task = tasks[names.index(_REFRESH)]
    assert task["ansible.builtin.systemd_service"] == {
        "name": "catena-version-check.service", "state": "started"}
    manifest = next(i for i, n in enumerate(names) if n.startswith("Release: record"))
    assert names.index(_REFRESH) < manifest, playbook


@pytest.mark.parametrize("playbook", ["converge.yml", "reconcile.yml"])
def test_the_include_carries_the_refresh_tag(playbook):
    play = yaml.safe_load((PLAYBOOKS / playbook).read_text(encoding="utf-8"))[0]
    include = next(t for t in play["post_tasks"]
                   if (t.get("ansible.builtin.include_tasks") or {}).get("file")
                   == "tasks/converge_tail.yml")
    assert "version_check" in include["tags"], playbook
