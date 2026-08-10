"""Lock down the catena-admin wiring for the backup-endpoint (S3) check.

The backup-endpoint counterpart of test_catena_admin_cloudflared_actions.py's
cloudflared-check coverage: a Go binary (payload/cmd/catena-backup-check,
shipped in the ungated host payload the same way catena-cloudflared-sync is)
validates a CANDIDATE S3 repository + access/secret key BEFORE the Settings
panel persists them. The candidate rides the SSH Runner's env map, never
$PAYLOAD/argv, so a mismatched env var name would silently break the whole
feature -- the host action would always see an empty candidate and the panel
would always be told the endpoint is invalid.

Run: uv run pytest tests/unit/test_catena_admin_backup_endpoint_actions.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

_ROLE = (
    Path(__file__).resolve().parents[3]
    / "ansible" / "roles" / "catena-admin"
)
DEFAULTS = _ROLE / "defaults" / "main.yml"
HOST = _ROLE / "tasks" / "host.yml"

ACTION = "backup-endpoint-check"
CANDIDATE_ENV_VARS = (
    "CATENA_BACKUP_CANDIDATE_REPO",
    "CATENA_BACKUP_CANDIDATE_ACCESS_KEY",
    "CATENA_BACKUP_CANDIDATE_SECRET_KEY",
)


def _defaults() -> dict:
    return yaml.safe_load(DEFAULTS.read_text())


def _ce_actions() -> dict[str, str]:
    return {a["name"]: a["shell"] for a in _defaults()["catena_admin_ce_reserved_actions"]}


def _ee_actions() -> dict[str, str]:
    return {a["name"]: a["shell"] for a in _defaults()["catena_admin_ee_reserved_actions"]}


def test_backup_endpoint_check_is_community():
    # The backup lane (like the tunnel) is a Community feature, so validating
    # a candidate endpoint before persist cannot be locked behind a licence.
    assert ACTION in _ce_actions()
    assert ACTION not in _ee_actions()


def test_backup_endpoint_check_reads_all_three_candidate_env_vars():
    shell = _ce_actions()[ACTION]
    for var in CANDIDATE_ENV_VARS:
        assert f"${var}" in shell, (
            f"{ACTION} does not read {var}; the catena-admin shell side "
            "(shell/web/backupendpoint.go) sends exactly these three names"
        )


def test_backup_endpoint_check_folds_candidate_into_the_binary_stdin_json():
    # payload/cmd/catena-backup-check reads {"repo","access_key","secret_key"}
    # on stdin -- jq (baseline-installed, roles/common) is what builds that
    # JSON from the env vars without a shell-injection risk from --arg.
    shell = _ce_actions()[ACTION]
    assert shell.strip().startswith("jq -n")
    assert "'{repo:$repo,access_key:$access_key,secret_key:$secret_key}'" in shell
    assert "{{ catena_admin_backup_check_bin }}" in shell


def test_backup_endpoint_check_never_uses_payload_or_argv():
    # Same "never argv" contract as cloudflared-check: the candidate travels
    # only through the env map the dispatch table reads by name.
    shell = _ce_actions()[ACTION]
    assert "$PAYLOAD" not in shell
    assert "base64" not in shell


def test_sshd_acceptenv_lists_the_backup_candidate_env_vars():
    passthrough = _defaults()["catena_admin_dispatch_env_passthrough"]
    for var in CANDIDATE_ENV_VARS:
        assert var in passthrough, (
            f"{var} missing from catena_admin_dispatch_env_passthrough -- "
            "sshd would drop it and the candidate would arrive empty"
        )


def test_the_candidate_env_survives_sudo_as_well_as_sshd():
    """Two hops, not one. sshd AcceptEnv lets the var into the SSH session;
    the forced command then runs the dispatcher under `sudo -n`, and sudo's
    env_reset drops the whole environment unless env_keep names the var.

    A var whitelisted on only one hop fails silently in the worst way: the
    action still runs, reads an empty string, and reports on nothing. That is
    how a valid endpoint gets rejected -- the check never saw it."""
    text = HOST.read_text()
    assert "env_keep" in text, (
        "the sudoers drop-in has no env_keep, so sudo's env_reset discards "
        "every candidate credential before the dispatcher reads it"
    )
    # Both hops must render from the same list, or they drift apart and the
    # drift is invisible until a live dispatch.
    assert text.count("catena_admin_dispatch_env_passthrough | join(' ')") == 2, (
        "AcceptEnv and env_keep must both render from "
        "catena_admin_dispatch_env_passthrough"
    )


def test_the_passthrough_list_stays_minimal():
    """Every name is a value the container can push into a root-run host
    process, so the list is the minimum the dispatch needs -- not a general
    env channel."""
    passthrough = _defaults()["catena_admin_dispatch_env_passthrough"]
    assert set(passthrough) == {
        "X_FORWARDED_EMAIL",
        "CATENA_CF_CANDIDATE_TOKEN",
        *CANDIDATE_ENV_VARS,
    }


def test_backup_check_binary_path_is_declared_once():
    d = _defaults()
    assert d["catena_admin_backup_check_bin"] == "/usr/local/bin/catena-backup-check"


def test_backup_check_binary_ships_via_the_blanket_payload_install():
    # Unlike catena-restic-key.py (a CE-owned script, individually copied by
    # host.yml), catena-backup-check is a Go engine binary from the
    # catena-admin image's payload/cmd/ tree, installed by roles/payload's
    # unconditional `for f in bin/*` loop -- so this role must NOT also
    # ship it via its own ansible.builtin.copy task (the AcceptEnv comment
    # block naming the binary in prose is fine; a `dest:` targeting it is not).
    text = HOST.read_text()
    assert "dest: /usr/local/bin/catena-backup-check" not in text
    assert "dest: {{ catena_admin_backup_check_bin }}" not in text
