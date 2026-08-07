"""Every systemd-run action must hand restic a cache location.

A transient systemd unit starts with a CLEAN environment. restic refuses to
run without one:

    unable to open cache: unable to locate cache directory:
    neither $XDG_CACHE_HOME nor $HOME are defined

Every other host action inherits the dispatcher's environment, where sudo
supplies HOME, so only the systemd-run ones are exposed -- and they are the
long-running ones (restore, migrate) that are wrapped precisely so a panel
restart cannot kill them.

This went unnoticed until `wizard_restore_smoke` first reached stage 4 in run
2026-08-01T14-47-54-569d: every restore started from the wizard died in about
a second. catena-backup.service had carried the fix, and the comment
explaining it, since it was written -- the lesson was learned once and applied
to one of the two places restic runs under systemd.

The parity assertion is the point of this file: the values must equal what
catena-backup.service sets, so both callers share one warm cache instead of
each filling its own, and so the next edit to either cannot silently split
them.

Run: uv run pytest tests/unit/test_catena_admin_systemd_run_env.py
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
DEFAULTS = ANSIBLE / "roles" / "catena-admin" / "defaults" / "main.yml"
BACKUP_UNIT = (
    ANSIBLE / "roles" / "backup" / "templates" / "catena-backup.service.j2"
)


def _actions() -> list[dict]:
    """Every action in every reserved-action list (CE, Business, cloudflared).

    Keyed on shape rather than on an exact variable name so a new list cannot
    be added and silently escape this gate.
    """
    d = yaml.safe_load(DEFAULTS.read_text())
    out: list[dict] = []
    for key, val in d.items():
        if not (key.startswith("catena_admin_") and key.endswith("_actions")):
            continue
        if isinstance(val, list):
            out.extend(a for a in val if isinstance(a, dict) and "name" in a)
    return out


def _systemd_run_actions() -> list[dict]:
    return [a for a in _actions() if "systemd-run" in str(a.get("shell", ""))]


def _backup_unit_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for line in BACKUP_UNIT.read_text().splitlines():
        m = re.match(r"\s*Environment=(HOME|XDG_CACHE_HOME)=(\S+)\s*$", line)
        if m:
            env[m.group(1)] = m.group(2)
    return env


def test_there_are_systemd_run_actions_to_check():
    """Guards the guard: if the actions are renamed away this file must fail
    loudly rather than pass over an empty set."""
    names = [a["name"] for a in _systemd_run_actions()]
    assert "catena-restore-run" in names
    assert "catena-migrate-run" in names


def test_every_systemd_run_action_sets_a_restic_cache_location():
    for action in _systemd_run_actions():
        shell = str(action["shell"])
        assert "--setenv=HOME=" in shell, (
            f"{action['name']} runs under systemd-run with no HOME; restic "
            "cannot open its cache and the action fails in about a second"
        )
        assert "--setenv=XDG_CACHE_HOME=" in shell, (
            f"{action['name']} runs under systemd-run with no XDG_CACHE_HOME"
        )


def test_the_cache_location_matches_catena_backup_service():
    """One warm cache, not one per caller -- and no silent divergence."""
    unit_env = _backup_unit_env()
    assert set(unit_env) == {"HOME", "XDG_CACHE_HOME"}, (
        f"catena-backup.service no longer sets both; read {unit_env}"
    )
    for action in _systemd_run_actions():
        shell = str(action["shell"])
        for var, want in unit_env.items():
            got = re.search(rf"--setenv={var}=(\S+)", shell)
            assert got, f"{action['name']} does not set {var}"
            assert got.group(1) == want, (
                f"{action['name']} points {var} at {got.group(1)} while "
                f"catena-backup.service uses {want}; they must share one cache"
            )


def test_the_setenv_precedes_the_binary():
    """systemd-run parses its own options up to the command; a --setenv after
    the binary is an argument TO the binary, not to systemd-run."""
    for action in _systemd_run_actions():
        shell = " ".join(str(action["shell"]).split())
        run_at = shell.index("systemd-run")
        setenv_at = shell.index("--setenv=HOME=", run_at)
        # The binary is the first {{ ... }} expansion after systemd-run.
        binary = re.search(r"\{\{\s*catena_\w+_bin\s*\}\}", shell[run_at:])
        assert binary, f"{action['name']}: no binary expansion found"
        assert setenv_at < run_at + binary.start(), (
            f"{action['name']}: --setenv comes after the binary, so systemd-run "
            "never sees it"
        )
