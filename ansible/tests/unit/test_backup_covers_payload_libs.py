"""A restore must not hand back a lane script without the modules it imports.

`backup_paths` carries /usr/local/bin, so every payload lane script rides the
snapshot. Their modules live in a second directory and, until run 1216, did
not. `nc_s3_hot_recovery` stage-4 restored a host that had
/usr/local/bin/catena-dashboard-sync and no /usr/local/lib/catena, which is
worse than having neither: the converge's payload gate reads the binary and
concludes the engines are present, wires the timer, starts the unit, and the
unit dies on `ModuleNotFoundError: No module named 'clients_provisioner'`.

The two directories are one artifact for backup purposes. This pins that, and
pins the lib path against the role that owns it rather than restating it, so
moving the lib dir cannot quietly drop it out of the snapshot.

Run: uv run pytest tests/unit/test_backup_covers_payload_libs.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
BACKUP_DEFAULTS = ANSIBLE / "reconcile" / "roles" / "backup" / "defaults" / "main.yml"
INFRA_DEFAULTS = ANSIBLE / "reconcile" / "roles" / "infrastructure" / "defaults" / "main.yml"

_LANE_BIN_DIR = "/usr/local/bin"


def _backup_paths() -> list[str]:
    return yaml.safe_load(BACKUP_DEFAULTS.read_text())["backup_paths"]


def _lib_dir() -> str:
    return yaml.safe_load(INFRA_DEFAULTS.read_text())["dashboard_sync_lib_dir"]


def test_the_lane_binaries_ride_the_snapshot():
    """The premise. If this ever stops being true the pairing below is moot,
    and dropping it should be a deliberate decision rather than a test that
    quietly starts proving nothing."""
    assert _LANE_BIN_DIR in _backup_paths()


def test_the_payload_lib_dir_rides_it_too():
    paths = _backup_paths()
    lib = _lib_dir()
    assert lib in paths, (
        f"{_LANE_BIN_DIR} is in backup_paths and {lib} is not. A restore would "
        "return the lane scripts without the modules they import at module "
        "scope, and the converge's payload gate cannot tell that host from a "
        "fully installed one"
    )


def test_the_backed_up_lib_dir_is_the_one_the_reconciler_reads():
    """Two spellings of the same directory would let one move and the other
    stay, which is the drift this test exists to prevent."""
    assert _lib_dir() == "/usr/local/lib/catena"
