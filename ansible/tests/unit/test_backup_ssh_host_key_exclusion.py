"""Lock the SSH-host-key exclusion on the backup path.

/etc/ssh is in backup_paths (for sshd_config + moduli + ssh_config), so
without an exclude the restic set captures ssh_host_*_key -- the host's
SSH IDENTITY and a private-key secret. backup_exclude_patterns drops
ssh_host_* so snapshots never store the host private key.

Run: uv run pytest tests/unit/test_backup_ssh_host_key_exclusion.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

_ROLE = Path(__file__).resolve().parents[3] / "ansible" / "reconcile" / "roles" / "backup"
_DEFAULTS = _ROLE / "defaults" / "main.yml"

_SSH_HOST_KEY_GLOB = "/etc/ssh/ssh_host_*"


def test_backup_defaults_exclude_ssh_host_keys():
    defaults = yaml.safe_load(_DEFAULTS.read_text())
    patterns = defaults["backup_exclude_patterns"]
    assert _SSH_HOST_KEY_GLOB in patterns, (
        "backup_exclude_patterns must drop ssh_host_* -- the host private "
        "key is a secret and must not ride the restic repo"
    )
