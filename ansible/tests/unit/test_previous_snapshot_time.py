"""Unit tests for the previous_snapshot_time filter plugin -- the staleness
floor roles/backup/tasks/restore.yml records in the post-restore marker.

The floor exists so the database replay can tell an archive that travelled in
the snapshot being restored from one an earlier pass left behind. Getting it
wrong in the strict direction refuses every legitimate archive; getting it
wrong in the loose direction replays yesterday's database over today's.

Run: uv run pytest tests/unit/test_previous_snapshot_time.py
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_PLUGIN = (
    Path(__file__).resolve().parents[3]
    / "ansible" / "playbooks" / "filter_plugins" / "previous_snapshot_time.py"
)
_spec = importlib.util.spec_from_file_location("previous_snapshot_time", _PLUGIN)
pst = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pst)

f = pst.previous_snapshot_time


def _snap(short_id: str, when: str) -> dict:
    return {
        "id": short_id + "0" * (64 - len(short_id)),
        "short_id": short_id,
        "time": when,
        "hostname": "vps1",
    }


THREE = [
    _snap("aaaaaaaa", "2026-07-20T03:00:00.123456789-04:00"),
    _snap("bbbbbbbb", "2026-07-21T03:00:00.123456789-04:00"),
    _snap("cccccccc", "2026-07-22T03:00:00.123456789-04:00"),
]


def test_picks_the_snapshot_before_the_one_named() -> None:
    assert f(json.dumps(THREE), "cccccccc") == "2026-07-21T07:00:00.123456Z"


def test_latest_means_the_newest_record() -> None:
    # restore_snapshot defaults to "latest", which names no id in the list.
    assert f(json.dumps(THREE), "latest") == "2026-07-21T07:00:00.123456Z"
    assert f(json.dumps(THREE), "") == "2026-07-21T07:00:00.123456Z"


def test_middle_snapshot_gets_the_one_below_it() -> None:
    assert f(json.dumps(THREE), "bbbbbbbb") == "2026-07-20T07:00:00.123456Z"


def test_a_full_id_resolves_like_its_short_form() -> None:
    full = THREE[2]["id"]
    assert f(json.dumps(THREE), full) == "2026-07-21T07:00:00.123456Z"


def test_the_first_snapshot_has_no_floor() -> None:
    # A repository with one pass cannot have left an earlier pass's archives
    # anywhere. Empty disables the guard, and that is the correct answer.
    assert f(json.dumps(THREE), "aaaaaaaa") == ""
    assert f(json.dumps([THREE[0]]), "latest") == ""


def test_unreadable_input_returns_empty_rather_than_raising() -> None:
    # restore.yml runs this with failed_when: false on the restic call, so a
    # repository that could not be listed must not take the restore down here.
    # It is reported loudly by the task that follows instead.
    assert f("", "latest") == ""
    assert f("not json", "latest") == ""
    assert f(json.dumps({"not": "a list"}), "latest") == ""
    assert f(json.dumps([{"no": "time"}]), "latest") == ""


def test_an_unknown_id_yields_no_floor() -> None:
    assert f(json.dumps(THREE), "dddddddd") == ""


def test_accepts_already_decoded_records() -> None:
    # ansible may hand back a parsed structure rather than a string.
    assert f(THREE, "cccccccc") == "2026-07-21T07:00:00.123456Z"


def test_utc_output_regardless_of_the_offset_restic_wrote() -> None:
    # The Go side parses this with time.RFC3339; normalising to Z keeps the
    # marker readable and the comparison unambiguous.
    utc = [
        _snap("aaaaaaaa", "2026-07-20T07:00:00Z"),
        _snap("bbbbbbbb", "2026-07-21T07:00:00Z"),
    ]
    assert f(json.dumps(utc), "bbbbbbbb") == "2026-07-20T07:00:00Z"
