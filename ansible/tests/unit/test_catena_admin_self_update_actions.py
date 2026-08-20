"""Lock the panel self-update dispatch wiring.

Two properties, both decisions rather than accidents.

  1. These are COMMUNITY actions. Withholding the mechanism leaves a
     free-edition host running whatever its install day happened to ship,
     forever -- a panel nobody has patched is the same shape of problem as
     a backup nobody has verified. What is licensed is the nightly chain
     that decides WHEN, not the ability to update at all.
  2. The self-update captures its health baseline into its OWN file. Both
     it and the container-bump lane record "what healthy looked like before
     the thing I am about to do"; sharing one file means a panel update
     overwrites the baseline a bump is still holding, and the diff that
     decides whether to roll back is taken against the wrong moment.

Run: uv run pytest tests/unit/test_catena_admin_self_update_actions.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

_ROLE = (
    Path(__file__).resolve().parents[3]
    / "ansible" / "roles" / "catena-admin"
)
DEFAULTS = _ROLE / "defaults" / "main.yml"

UPDATE = "catena-admin-self-update"
STATUS = "catena-admin-self-update-status"


def _defaults() -> dict:
    return yaml.safe_load(DEFAULTS.read_text())


def _ce_actions() -> dict[str, str]:
    return {a["name"]: a["shell"] for a in _defaults()["catena_admin_ce_reserved_actions"]}


def test_self_update_is_community_not_business():
    ce = _ce_actions()
    assert {UPDATE, STATUS} <= set(ce)

    ee = {a["name"] for a in _defaults()["catena_admin_ee_reserved_actions"]}
    assert not {UPDATE, STATUS} & ee, (
        "a Community host that cannot update its own panel runs its install "
        "day's build forever; the licence gates the chain that decides when, "
        "not the ability to update"
    )


def test_the_update_is_reserved_never_catalogued():
    """The shell-read catalog would render it as a raw Actions button whose
    argument is a base64 blob typed by hand. The Panel-updates page is the
    surface; the dispatch table is all this needs."""
    catalog = yaml.safe_load(
        (_ROLE / "templates" / "actions.yml.j2").read_text().split("\n", 1)[1]
    )
    names = {a["name"] for a in (catalog or {}).get("actions", [])}
    assert not {UPDATE, STATUS} & names


def test_the_update_runs_detached_under_its_own_unit():
    """The operation kills the container that asked for it, so the request
    cannot be the thing that waits for the answer."""
    shell = _ce_actions()[UPDATE]
    assert "systemd-run" in shell
    assert "--unit catena-admin-selfupdate" in shell
    assert "--pipe" not in shell, (
        "there is nothing left to stream to once the panel is down"
    )


def test_the_update_takes_the_shared_lock():
    shell = _ce_actions()[UPDATE]
    assert "flock" in shell and "/run/catena.lock" in shell, (
        "an update running alongside the nightly chain races its health "
        "baseline, and a baseline captured mid-roll excuses the regression "
        "it was meant to catch"
    )


def test_the_baseline_is_not_the_container_lanes():
    defaults = _defaults()
    baseline = defaults["catena_admin_selfupdate_baseline_file"]
    assert baseline != "/var/lib/catena/stack-update-baseline.json"
    assert "SELF_UPDATE_BASELINE_FILE=" in _ce_actions()[UPDATE], (
        "without the override the engine falls back to its compiled default; "
        "pinning it here is what keeps the converge and the engine agreeing "
        "on one path"
    )


def test_the_image_travels_encoded_not_interpolated():
    shell = _ce_actions()[UPDATE]
    assert "base64 -d" in shell
    assert 'SELF_UPDATE_IMAGE="$(' in shell, (
        "an unquoted substitution splits a reference containing a space into "
        "two arguments"
    )


def test_status_reads_three_things_and_fails_none_of_them():
    """Every substitution legitimately produces nothing on a host that has
    never updated. A status action that exited non-zero there would report a
    fault on every fresh install."""
    shell = _ce_actions()[STATUS]
    for key in ("state=", "unit=", "image="):
        assert key in shell
    assert shell.count("|| true") + shell.count("|| echo") >= 3
