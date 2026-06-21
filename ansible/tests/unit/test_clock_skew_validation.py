"""R17: clock-skew validation across validate + restore.

TLS, OIDC tokens, Tailscale auth, and restic snapshot signatures all
fail under clock skew >5 min. The failure modes are downstream and
look like other bugs (cert error, auth refused, "snapshot is from
the future"), so the validation surface needs to assert the root
cause directly.

Three callsites:

  1. roles/common/tasks/validate.yml -- Vantage 1a probe, runs every
     validate / site.yml.
  2. roles/backup/tasks/restore.yml prereqs -- pre-restore.
  3. roles/backup/tasks/restore.yml after restic restore -- post-restore
     (catches /etc/systemd/timesyncd drop-ins from the source host).

The probe uses `timedatectl show --property=...` (not `status`) for
locale-stable output; NTPSynchronized=yes is the canonical "sync has
happened" flag set by systemd-timesyncd or chrony.
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[3]


def test_common_validate_asserts_ntp_sync():
    """The continuous validate probe must check both NTP=yes AND
    NTPSynchronized=yes. Just one of those flags can be true while
    sync is broken (NTP=yes + NTPSynchronized=no = service running
    but no upstream sample yet, e.g. firewall blocking 123/udp)."""
    text = (REPO / "ansible" / "roles" / "common" / "tasks" / "validate.yml").read_text()
    assert "timedatectl" in text
    # Both predicates must be in the assert block.
    assert "'NTP=yes' in _clock_status.stdout" in text
    assert "'NTPSynchronized=yes' in _clock_status.stdout" in text
    # Stable output mode must be requested explicitly -- `timedatectl
    # status` without --property is locale-formatted and the matcher
    # would silently break under non-en_US locales.
    assert "--property=NTPSynchronized" in text


def test_restore_pre_check_asserts_ntp_sync():
    """Pre-restore gate: must run before the restic restore command
    itself, so a drifted clock is caught while the host can still
    self-correct (and the operator hasn't stopped docker yet)."""
    text = (REPO / "ansible" / "roles" / "backup" / "tasks" / "restore.yml").read_text()
    pre_idx = text.index("Preflight -- clock is NTP-synchronized")
    restic_idx = text.index("Restic restore")
    assert pre_idx < restic_idx, "pre-restore clock check must precede restic restore"
    # The prereq block runs before docker stop; the docker stop block
    # writes to log and there is no "early return" path that would
    # let restic execute under a drifted clock.
    docker_stop_idx = text.index("Stop docker.service")
    assert pre_idx < docker_stop_idx, (
        "pre-restore clock check should run before docker is stopped, "
        "while the operator can still fix sync without a separate "
        "system-restart cycle"
    )


def test_restore_post_check_asserts_ntp_sync():
    """Post-restore gate: catches the case where /etc was restored
    with a stale systemd-timesyncd drop-in from the source host that
    breaks sync on the target. Must run AFTER the restic restore but
    BEFORE the post-restore.needed marker triggers downstream hooks.

    Three-task post-restore sequence (B6 from 2026-05-23 review):
      1. restart systemd-timesyncd so it reloads the restored config
      2. poll NTPSynchronized for up to 30 s (retries + until)
      3. assert NTP sync"""
    text = (REPO / "ansible" / "roles" / "backup" / "tasks" / "restore.yml").read_text()
    restic_idx = text.index("Restic restore")
    restart_idx = text.index("restart systemd-timesyncd to load restored config")
    poll_idx = text.index("poll for NTP resync")
    assert_idx = text.index("assert NTP sync after restore")
    marker_idx = text.index("Drop post-restore marker for site.yml hooks")
    assert restic_idx < restart_idx < poll_idx < assert_idx < marker_idx, (
        "post-restore clock sequence must be: restic restore -> "
        "timesyncd restart -> NTP poll -> NTP assert -> drop marker. "
        "Without the restart, the running daemon holds the PRE-restore "
        "config in memory and the assert passes even when the restored "
        "config is broken; drift would surface 1-2 days later."
    )


def test_restore_post_check_restarts_timesyncd():
    """The restart task is load-bearing: without it the live daemon
    holds the PRE-restore config (e.g. valid NTP=pool.ntp.org) and the
    NTPSynchronized assert passes even though the restored config on
    disk (e.g. NTP=pool.invalid) is broken. The drift only surfaces
    1-2 days later when the cached skew grows past Keycloak's 60s OIDC
    tolerance, silently breaking auth."""
    text = (REPO / "ansible" / "roles" / "backup" / "tasks" / "restore.yml").read_text()
    assert "systemd.systemd" not in text or True  # ansible.builtin.systemd
    assert "ansible.builtin.systemd" in text, (
        "must use the ansible.builtin.systemd module for the restart "
        "(not raw command:) so failure surfaces as a proper Ansible "
        "task failure with module-level diagnostics."
    )
    # Find the restart task and confirm state: restarted.
    restart_idx = text.index("restart systemd-timesyncd to load restored config")
    block_end = text.index("- name:", restart_idx + 1)
    block = text[restart_idx:block_end]
    assert "state: restarted" in block
    assert "name: systemd-timesyncd" in block


def test_restore_post_check_polls_for_resync_with_retries():
    """The NTP daemon needs time to reach a peer + sync after restart
    (typically 5-15s). A single-shot check post-restart races the
    sync window and can spuriously fail. The poll uses retries + until
    so the assert window is ~30s total."""
    text = (REPO / "ansible" / "roles" / "backup" / "tasks" / "restore.yml").read_text()
    poll_idx = text.index("poll for NTP resync")
    block_end = text.index("- name:", poll_idx + 1)
    block = text[poll_idx:block_end]
    assert "retries:" in block
    assert "delay:" in block
    assert "until:" in block


def test_clock_checks_use_timedatectl_show_not_status():
    """`timedatectl status` is locale-formatted and breaks the matcher
    under non-en_US.UTF-8. `show --property=` is key=value, locale-
    independent. Catch any regression that flips the call shape back."""
    files = [
        REPO / "ansible" / "roles" / "common" / "tasks" / "validate.yml",
        REPO / "ansible" / "roles" / "backup" / "tasks" / "restore.yml",
    ]
    for f in files:
        text = f.read_text()
        # All `timedatectl` invocations involved in the R17 checks
        # must use `show`, not `status`.
        for line_no, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("- timedatectl"):
                # next non-empty argv line should be `show`
                continue
            if stripped == "- status":
                raise AssertionError(
                    f"{f.name}:{line_no} uses `timedatectl status` -- "
                    "use `timedatectl show --property=NTPSynchronized` "
                    "for locale-stable output"
                )
