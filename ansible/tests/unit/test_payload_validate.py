"""reconcile/roles/payload/tasks/validate.yml checks that the installed-image
marker holds the ID of the image this tree runs, and, as negative checks, that
the stage directory root installs from is root's alone and the templates catalog
cache is gone.

Run: uv run pytest tests/unit/test_payload_validate.py
"""
from __future__ import annotations

import yaml

from ansible_jinja import b64, holds
from ansible_tree import role_dir, walk_tasks

TASKS = walk_tasks(yaml.safe_load((role_dir("payload") / "tasks" / "validate.yml").read_text()))
ASSERT = "ansible.builtin.assert"


def _task(fragment: str) -> dict:
    for t in TASKS:
        if fragment in (t.get("name") or ""):
            return t
    raise AssertionError(f"no task whose name contains {fragment!r}")


def _marker(marker: str, rc: int = 0, image_id: str = "sha256:aaa") -> bool:
    return holds(
        _task("came from the image this tree runs"), ASSERT,
        _payload_val_marker={"content": b64(marker + "\n")},
        _payload_val_image_id={"rc": rc, "stdout": image_id + "\n"},
    )


def test_the_marker_must_hold_the_image_id_docker_answers():
    assert _marker("sha256:aaa")
    assert not _marker("sha256:bbb")
    assert not _marker("sha256:aaa", rc=1)


def test_a_missing_marker_fails():
    assert not holds(
        _task("came from the image this tree runs"), ASSERT,
        _payload_val_marker={"failed": True},
        _payload_val_image_id={"rc": 0, "stdout": "sha256:aaa"},
    )


def test_the_marker_is_checked_only_when_the_converge_installs_the_engines():
    (block,) = [t for t in TASKS if t.get("block")]
    assert block["when"] == "catena_payload_install | bool"
    out_of_band = _task("engines are staged out of band")
    assert out_of_band["when"] == "not (catena_payload_install | bool)"
    assert "not checked" in out_of_band["ansible.builtin.debug"]["msg"]


def _stage(**stat: object) -> bool:
    base = {"exists": True, "isdir": True, "pw_name": "root", "gr_name": "root", "mode": "0700"}
    return holds(_task("stage directory is root's alone"), ASSERT,
                 _payload_val_stage={"stat": {**base, **stat}})


def test_the_stage_directory_must_be_root_0700():
    assert _stage()
    assert not _stage(mode="0755")
    assert not _stage(mode="0770")
    assert not _stage(pw_name="catena")
    assert not _stage(gr_name="docker")
    assert not _stage(isdir=False)


def test_an_absent_stage_directory_is_not_checked():
    assert _task("stage directory is root's alone")["when"] == "_payload_val_stage.stat.exists"


def test_the_catalog_cache_must_be_absent():
    task = _task("templates catalog cache is gone")
    assert holds(task, ASSERT, _payload_val_catalog={"stat": {"exists": False}})
    assert not holds(task, ASSERT, _payload_val_catalog={"stat": {"exists": True}})
    stat = _task("stat the templates catalog cache")
    assert stat["ansible.builtin.stat"]["path"] == "/var/lib/catena/templates-catalog.json"
