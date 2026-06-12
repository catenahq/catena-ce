"""Unit tests for the keyscan_alias filter used by roles/catena-admin/
tasks/host.yml to build the admin container's known_hosts.

The container's SSH runner only ever connects to host.docker.internal, so
the role uses replace=True to emit "host.docker.internal keytype keydata"
-- dropping the scanned host, which is the only volatile part of the
content and made the copy non-idempotent across converges (ansible_host
flips public-IP <-> tailnet-IP, and differs after a cross-host restore).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ANSIBLE_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ANSIBLE_DIR / "playbooks" / "filter_plugins"))

from keyscan_alias import (  # noqa: E402
    FilterModule,
    keyscan_alias,
    keyscan_alias_lines,
)


def test_aliases_single_entry_prefix_default():
    out = keyscan_alias_lines(
        ["100.64.0.10 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5"],
        "host.docker.internal",
    )
    assert out == [
        "host.docker.internal,100.64.0.10 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5",
    ]


def test_replace_drops_scanned_host():
    out = keyscan_alias_lines(
        ["100.64.0.10 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5"],
        "host.docker.internal",
        replace=True,
    )
    assert out == [
        "host.docker.internal ssh-ed25519 AAAAC3NzaC1lZDI1NTE5",
    ]


def test_replace_is_idempotent_across_scanned_host_change():
    # The load-bearing property: with replace=True the rendered content is
    # IDENTICAL regardless of which address ssh-keyscan saw, so the copy task
    # stays idempotent when ansible_host flips public-IP <-> tailnet-IP or
    # changes after a cross-host restore.
    key = "ssh-ed25519 AAAAKEYDATA"
    public = keyscan_alias([f"203.0.113.7 {key}"], "host.docker.internal", replace=True)
    tailnet = keyscan_alias([f"100.64.0.10 {key}"], "host.docker.internal", replace=True)
    assert public == tailnet == "host.docker.internal ssh-ed25519 AAAAKEYDATA\n"


def test_replace_supports_tab_separator():
    out = keyscan_alias_lines(
        ["host.example\tssh-rsa\tAAAA"], "alias.example", replace=True,
    )
    assert out == ["alias.example\tssh-rsa\tAAAA"]


def test_preserves_comment_lines_in_replace_mode():
    lines = [
        "# 100.64.0.10:22 SSH-2.0-OpenSSH_9.2p1",
        "100.64.0.10 ssh-ed25519 AAAAKEY",
    ]
    out = keyscan_alias_lines(lines, "host.docker.internal", replace=True)
    assert out == [
        "# 100.64.0.10:22 SSH-2.0-OpenSSH_9.2p1",
        "host.docker.internal ssh-ed25519 AAAAKEY",
    ]


def test_passthrough_when_no_separator():
    assert keyscan_alias_lines(["singletoken"], "alias", replace=True) == ["singletoken"]


def test_none_returns_empty_list():
    assert keyscan_alias_lines(None, "alias", replace=True) == []


def test_empty_alias_rejected():
    with pytest.raises(ValueError):
        keyscan_alias_lines(["a b c"], "", replace=True)


def test_string_form_trailing_newline_replace():
    out = keyscan_alias(
        [
            "# 100.64.0.10:22 SSH-2.0-OpenSSH_9.2p1",
            "100.64.0.10 ssh-ed25519 AAAAKEY",
        ],
        "host.docker.internal",
        replace=True,
    )
    assert out == (
        "# 100.64.0.10:22 SSH-2.0-OpenSSH_9.2p1\n"
        "host.docker.internal ssh-ed25519 AAAAKEY\n"
    )


def test_filter_module_registers_under_expected_names():
    assert FilterModule().filters() == {
        "keyscan_alias": keyscan_alias,
        "keyscan_alias_lines": keyscan_alias_lines,
    }
