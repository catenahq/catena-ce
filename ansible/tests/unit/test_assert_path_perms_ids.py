"""bootstrap/roles/common/tasks/_assert_path_perms.yml compares an optional
`uid` and `gid` as integers, because stat sets no owner or group name for an id
with no passwd or group entry (the container's 65532).

Run: uv run pytest tests/unit/test_assert_path_perms_ids.py
"""
from __future__ import annotations

import jinja2
import yaml

from ansible_tree import role_dir

TASKS = yaml.safe_load((role_dir("common") / "tasks" / "_assert_path_perms.yml").read_text())
THAT = next(t for t in TASKS if "ansible.builtin.assert" in t)["ansible.builtin.assert"]["that"]
ENV = jinja2.Environment()


def _holds(entry: dict, **stat: object) -> bool:
    item = {"item": entry, "stat": {"exists": True, "isdir": True, "mode": "0700", **stat}}
    return all(ENV.compile_expression(expr)(item=item) for expr in THAT)


def test_a_matching_uid_and_gid_pass_even_as_strings_and_with_no_names():
    entry = {"path": "/x", "type": "directory", "uid": "65532", "gid": "65532", "mode": "0700"}
    assert _holds(entry, uid=65532, gid=65532)


def test_a_wrong_uid_fails():
    assert not _holds({"path": "/x", "uid": "65532"}, uid=0, gid=65532)


def test_a_wrong_gid_fails():
    assert not _holds({"path": "/x", "gid": "1000"}, uid=0, gid=0)


def test_ids_are_not_checked_when_not_given():
    assert _holds({"path": "/x", "type": "directory"}, uid=1, gid=2)
