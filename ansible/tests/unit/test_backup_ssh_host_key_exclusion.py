"""Lock the SSH-host-key exclusion on both the backup and restore paths.

/etc/ssh is in backup_paths (for sshd_config + moduli + ssh_config), so
without an exclude the restic set captures ssh_host_*_key -- the host's
SSH IDENTITY and a private-key secret. On a fresh-host DR restore, restic
restore --target / then overwrites the recovered box's own cloud-init
host keys with the source's, cloning the dead host's SSH identity. That
collides with the controller known_hosts entry TOFU-ed during bootstrap
(ansible.cfg StrictHostKeyChecking=accept-new errors on the changed key),
so validate.yml Vantage 3 aborts UNREACHABLE ("REMOTE HOST
IDENTIFICATION HAS CHANGED") -- observed in the bench decommission_recovery
scenario.

Two independent guards, both pinned here:
  - backup:  backup_exclude_patterns drops ssh_host_* so new snapshots
             never store the host private key.
  - restore: the restic restore argv excludes ssh_host_* so even an
             OLDER snapshot that still holds them can never overwrite a
             recovered box's own keys.

Run: uv run pytest tests/unit/test_backup_ssh_host_key_exclusion.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

_ROLE = Path(__file__).resolve().parents[3] / "ansible" / "reconcile" / "roles" / "backup"
_DEFAULTS = _ROLE / "defaults" / "main.yml"
_RESTORE = _ROLE / "tasks" / "restore.yml"

_SSH_HOST_KEY_GLOB = "/etc/ssh/ssh_host_*"


def test_backup_defaults_exclude_ssh_host_keys():
    defaults = yaml.safe_load(_DEFAULTS.read_text())
    patterns = defaults["backup_exclude_patterns"]
    assert _SSH_HOST_KEY_GLOB in patterns, (
        "backup_exclude_patterns must drop ssh_host_* -- the host private "
        "key is a secret and must not ride the restic repo"
    )


def test_restore_excludes_ssh_host_keys():
    tasks = yaml.safe_load(_RESTORE.read_text())
    restore = next(
        (t for t in tasks if "Restic restore into" in (t.get("name") or "")),
        None,
    )
    assert restore is not None, "the restic restore task went missing"
    argv = restore["ansible.builtin.command"]["argv"]
    # Every exclude is the item AFTER an "--exclude" token.
    excludes = [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "--exclude"]
    assert _SSH_HOST_KEY_GLOB in excludes, (
        "restore must exclude ssh_host_* so a legacy snapshot cannot "
        "overwrite the recovered box's own host keys (the DR identity clone "
        "that aborts validate.yml Vantage 3)"
    )
