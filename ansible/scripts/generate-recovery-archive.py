#!/usr/bin/env python3
"""Host-side recovery-archive generator. Produces an AES-256
password-protected .zip containing the full client-recovery toolkit
(recover.sh, restore.sh, envelope.env, vault.recovered.yml, EN+FR
READMEs) and drops it under /var/backups/catena-export/, where the
recovery.<zone> nginx sidecar serves it behind oauth2-proxy's admin
gate.

Triggered by the catena-admin "Generate recovery archive (encrypted)"
action. The action forwards the passphrase typed into its dialog as
the $PAYLOAD env var; the operator hands the SAME passphrase to the
client out-of-band (SMS / phone / encrypted message). Single secret,
single delivery channel; the artifact itself never leaves the
admin-gated download URL.
"""
# Managed by Ansible (roles/infrastructure). Do not edit by hand.
# /usr/local/bin/catena-generate-recovery-archive
#
# Runs as root via sudo (catena-admin dispatches it over SSH from the
# Recovery tab). Root is needed because:
#   - extract_secrets_core probes /etc/catena/restic.pass (mode 0600
#     root) and /run/secrets/postgres_password inside the dokploy-
#     postgres container.
#   - /etc/catena/backup.env is mode 0600 root.
#   - The output .zip is mode 0600 root and lives under
#     /var/backups/catena-export/ which is mode 0750 root:1000 (group
#     1000 = catena-admin container UID so the /recovery tab can list).
#
# Stdlib + 7z binary only. The role installs:
#   /usr/local/lib/catena/extract_secrets_core.py  (vault recovery probes)
#   /usr/local/lib/catena/recovery_archive_core.py (envelope + zip)
#   /usr/local/lib/catena/restore.sh               (the recovery script)
#
# Stdout is the only thing the operator sees in catena-admin's run
# panel, so we never echo the passphrase, never echo recovered values.
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

CORE_DIR = "/usr/local/lib/catena"
if CORE_DIR not in sys.path:
    sys.path.insert(0, CORE_DIR)

import extract_secrets_core as core  # noqa: E402
import recovery_archive_core as rac  # noqa: E402

EXPORT_DIR = Path("/var/backups/catena-export")
RESTORE_SH_PATH = Path("/usr/local/lib/catena/restore.sh")
BACKUP_ENV_PATH = Path("/etc/catena/backup.env")
KEEP_LAST = 3
ARCHIVE_GLOB = "recovery-*.zip"


def _run(cmd: str) -> core.CommandResult:
    """Run a shell command locally; return a CommandResult.

    Mirrors vps-scripts/extract-secrets.py::_run -- shell=True because
    the LOCATIONS probes are real shell pipelines (`docker exec ... |
    jq -r ...`), not user input. Operator-controlled, no injection
    surface.
    """
    try:
        res = subprocess.run(  # nosec B602 - operator-controlled probes
            # nosemgrep: python.lang.security.audit.subprocess-shell-true.subprocess-shell-true
            cmd, shell=True, capture_output=True, text=True, timeout=30,
            executable="/bin/bash",
        )
    except subprocess.TimeoutExpired as exc:
        return core.CommandResult(124, exc.stdout or "", f"timeout: {cmd}")
    return core.CommandResult(res.returncode, res.stdout, res.stderr)


def _read_backup_env() -> dict[str, str]:
    """Parse /etc/catena/backup.env into a dict. Stdlib KEY=VALUE
    parser: same shape as helpers/extract_secrets_core.py::_parse_env
    but operating on the whole file. Empty file or missing path
    returns {} so build_envelope falls through cleanly."""
    if not BACKUP_ENV_PATH.is_file():
        return {}
    out: dict[str, str] = {}
    try:
        text = BACKUP_ENV_PATH.read_text()
    except PermissionError:
        # Should not happen when running as root; defensive in case the
        # operator ran the script under sudo with a env_keep that lost
        # something. Surface instead of crashing.
        print(
            f"error: cannot read {BACKUP_ENV_PATH} (run as root via sudo)",
            file=sys.stderr,
        )
        return {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        out[key.strip()] = value
    return out


def _rotate_old_archives() -> None:
    """Keep the N most-recent recovery zip files under EXPORT_DIR;
    delete the rest. Non-fatal: a failed unlink (mode/ownership weirdness)
    just logs and continues."""
    if not EXPORT_DIR.is_dir():
        return
    archives = sorted(
        EXPORT_DIR.glob(ARCHIVE_GLOB),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for stale in archives[KEEP_LAST:]:
        try:
            stale.unlink()
        except OSError:
            pass


def main() -> int:
    passphrase = os.environ.get("PAYLOAD", "").strip()
    if not passphrase:
        print(
            "error: no passphrase provided. Retry the button and type a "
            "passphrase in the dialog.",
            file=sys.stderr,
        )
        return 2

    if os.geteuid() != 0:
        print(
            "error: must run as root (the button wires this through sudo).",
            file=sys.stderr,
        )
        return 1

    if shutil.which("7z") is None and shutil.which("7zz") is None:
        print(
            "error: 7z is not installed. Install p7zip-full "
            "(`apt-get install -y p7zip-full`) and retry.",
            file=sys.stderr,
        )
        return 1

    # 1. Reconstruct vault from live host state.
    extract_result = core.extract(_run)
    n_ok = len(extract_result.values)
    n_fail = len(extract_result.failures)
    n_omit = len(extract_result.omitted)

    # 2. Read /etc/catena/backup.env for the bucket URLs.
    backup_env = _read_backup_env()
    if not backup_env.get("RESTIC_REPOSITORY"):
        print(
            f"error: could not read RESTIC_REPOSITORY from "
            f"{BACKUP_ENV_PATH}; the recovery archive would be useless. "
            "Make sure roles/backup has converged on this host.",
            file=sys.stderr,
        )
        return 1

    # 3. Build the 16-var envelope. cloudflared_token is operator
    # decision and not on the VPS; restore.sh prompts inline if the
    # recipient asks for it (the existing prompt path).
    envelope = rac.build_envelope(
        vault=extract_result.values,
        backup_env=backup_env,
        cloudflared_token="",
    )
    if not envelope["RESTIC_PASSWORD"]:
        print(
            "error: vault_backup_restic_password could not be recovered "
            f"from this host. extract_secrets_core failures: "
            f"{list(extract_result.failures)}",
            file=sys.stderr,
        )
        return 1

    # 4. Read the in-place restore.sh template the role vendored.
    if not RESTORE_SH_PATH.is_file():
        print(
            f"error: {RESTORE_SH_PATH} is missing. The "
            "roles/infrastructure recovery_archive task should have "
            "installed it; run a converge.",
            file=sys.stderr,
        )
        return 1
    restore_sh = RESTORE_SH_PATH.read_text()

    # 5. Reconstructed vault yaml (extract_secrets_core's emission --
    # carries provider-only entries as commented placeholders, so the
    # client knows which keys to re-mint vs which were recovered).
    vault_yml = core.to_yaml(extract_result)

    # 6. Pack. roles/infrastructure manages EXPORT_DIR's mode + ownership;
    # mkdir-with-exist_ok is a belt-and-suspenders fallback for first-
    # ever invocation. Done HERE (not at the top of main) so an early
    # die-fast on missing inputs does not touch the filesystem.
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    hostname = os.uname().nodename
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_path = EXPORT_DIR / f"recovery-{ts}.zip"
    members = rac.build_archive_members(
        envelope=envelope,
        restore_sh=restore_sh,
        vault_recovered_yml=vault_yml,
        hostname=hostname,
        generated_at=ts,
    )
    try:
        rac.pack_recovery_archive(
            out_path=archive_path,
            members=members,
            passphrase=passphrase,
        )
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    archive_path.chmod(0o600)
    size_kb = max(1, archive_path.stat().st_size // 1024)
    _rotate_old_archives()

    # Client-facing output: NO secret material, only retrieval + decrypt
    # hints. The operator who clicked the button is the only one who
    # sees this output; they typed the passphrase, so they already know
    # it -- never echo it back.
    recovery_url = os.environ.get("RECOVERY_URL", "").strip()
    n_pop = sum(1 for v in envelope.values() if v)
    print("Recovery archive ready.")
    print()
    print(f"  File         : {archive_path.name}")
    print(f"  Size         : ~{size_kb} KB")
    print(f"  Vault state  : recovered={n_ok}, failed={n_fail}, omitted={n_omit}")
    print(f"  Envelope     : {n_pop}/{len(envelope)} vars populated")
    print()
    if recovery_url:
        print("To download (admin-gated; operators only):")
        print(f"  1. Open {recovery_url} in your browser.")
        print("  2. Sign in as an administrator.")
        print(f"  3. Click {archive_path.name} to save it.")
        print()
        print("Operator fallback (tailnet scp -- requires operator access):")
    else:
        print("To retrieve off-host via ssh (tailnet or ops access):")
    print(f"  scp ops@{hostname}:{archive_path} .")
    print()
    print("Hand the recovery passphrase to the recipient out-of-band")
    print("(SMS, phone, encrypted message). Then the recipient:")
    print(f"  1. Extracts {archive_path.name} with the passphrase.")
    print("  2. Reads README.txt (or LISEZ-MOI.txt for French).")
    print("  3. Runs `sudo ./recover.sh` on a fresh Debian/Ubuntu VPS.")
    print()
    if n_fail:
        print(f"NOTE: {n_fail} vault key(s) could not be recovered from "
              "this host -- they appear as RECOVERY-FAILED markers in "
              "vault.recovered.yml. Review before handing off.")
    print(f"Older archives cleaned (keeping last {KEEP_LAST}).")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        sys.exit(130)
