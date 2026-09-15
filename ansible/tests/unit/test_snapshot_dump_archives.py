"""Unit tests for the snapshot_dump_archives filter plugin -- the manifest
reconcile/roles/backup/tasks/restore.yml records in the post-restore marker.

The manifest is what admits a database archive to the replay. Getting it wrong
in the strict direction refuses every legitimate archive and fails the
recovery; getting it wrong in the loose direction replays an earlier pass's
database over freshly restored volumes.

Run: uv run pytest tests/unit/test_snapshot_dump_archives.py
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_PLUGIN = (
    Path(__file__).resolve().parents[3]
    / "ansible" / "playbooks" / "filter_plugins" / "snapshot_dump_archives.py"
)
_spec = importlib.util.spec_from_file_location("snapshot_dump_archives", _PLUGIN)
sda = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sda)

f = sda.snapshot_dump_archives

PG = "/mnt/data/backup-staging/pg"


def _header() -> str:
    return json.dumps({
        "time": "2026-08-13T22:23:21Z", "tree": "abc", "paths": ["/mnt/data"],
        "hostname": "testvm-b", "struct_type": "snapshot",
    })


def _node(name: str, node_type: str = "file", parent: str = PG) -> str:
    return json.dumps({
        "name": name, "type": node_type, "path": f"{parent}/{name}",
        "uid": 0, "gid": 0, "size": 1024, "struct_type": "node",
    })


def test_returns_the_file_basenames_sorted() -> None:
    out = "\n".join([
        _header(),
        _node("nextcloud-db-1-20260813T213429Z.sql.gz"),
        _node("catena-postgres-20260813T213429Z.sql.gz"),
    ])
    assert f(out, PG) == [
        "catena-postgres-20260813T213429Z.sql.gz",
        "nextcloud-db-1-20260813T213429Z.sql.gz",
    ]


def test_the_snapshot_header_is_not_an_archive() -> None:
    # It carries no "name", but it does carry "paths", and a reader that went
    # looking for path-ish fields could mistake it for a node.
    assert f(_header(), PG) == []


def test_directories_are_not_archives() -> None:
    out = "\n".join([_header(), _node("older", node_type="dir")])
    assert f(out, PG) == []


def test_a_nested_files_basename_is_not_admitted() -> None:
    # An archive of the same name one directory down is a different file, and
    # the replay only ever reads the top level.
    out = "\n".join([
        _header(),
        _node("nextcloud-db-1-20260813T213429Z.sql.gz", parent=f"{PG}/archive"),
    ])
    assert f(out, PG) == []


def test_a_trailing_slash_on_the_directory_still_matches() -> None:
    out = "\n".join([_header(), _node("app-db-20260813T213429Z.sql.gz")])
    assert f(out, PG + "/") == ["app-db-20260813T213429Z.sql.gz"]


def test_an_unparseable_line_is_skipped_not_fatal() -> None:
    # restic has added fields to this stream before. One line nobody recognises
    # must not take a restore down, and must not silently empty the manifest.
    out = "\n".join([
        _header(),
        "{not json at all",
        _node("app-db-20260813T213429Z.sql.gz"),
    ])
    assert f(out, PG) == ["app-db-20260813T213429Z.sql.gz"]


def test_empty_input_yields_an_empty_list() -> None:
    # The caller gates on restic's rc before using this: an unreadable listing
    # and a snapshot carrying nothing both arrive here as no usable lines, and
    # only the first may disable the guard.
    assert f("", PG) == []
    assert f(None, PG) == []
    assert f(b"", PG) == []


def test_bytes_input_is_decoded() -> None:
    out = "\n".join([_header(), _node("app-db-20260813T213429Z.sql.gz")])
    assert f(out.encode(), PG) == ["app-db-20260813T213429Z.sql.gz"]


def test_a_duplicate_name_is_reported_once() -> None:
    out = "\n".join([
        _header(),
        _node("app-db-20260813T213429Z.sql.gz"),
        _node("app-db-20260813T213429Z.sql.gz"),
    ])
    assert f(out, PG) == ["app-db-20260813T213429Z.sql.gz"]
