"""R17: clock-skew validation in validate.

TLS, OIDC tokens, Tailscale auth, and restic snapshot signatures all
fail under clock skew >5 min. The failure modes are downstream and
look like other bugs (cert error, auth refused, "snapshot is from
the future"), so the validation surface needs to assert the root
cause directly: bootstrap/roles/common/tasks/validate.yml's Vantage 1a
probe, run on every validate / converge.

The probe uses `timedatectl show --property=...` (not `status`) for
locale-stable output; NTPSynchronized=yes is the canonical "sync has
happened" flag set by systemd-timesyncd or chrony.
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
_VALIDATE = REPO / "ansible" / "bootstrap" / "roles" / "common" / "tasks" / "validate.yml"


def test_common_validate_asserts_ntp_sync():
    """The continuous validate probe must check both NTP=yes AND
    NTPSynchronized=yes. Just one of those flags can be true while
    sync is broken (NTP=yes + NTPSynchronized=no = service running
    but no upstream sample yet, e.g. firewall blocking 123/udp)."""
    text = _VALIDATE.read_text()
    assert "timedatectl" in text
    # Both predicates must be in the assert block.
    assert "'NTP=yes' in _clock_status.stdout" in text
    assert "'NTPSynchronized=yes' in _clock_status.stdout" in text
    # Stable output mode must be requested explicitly -- `timedatectl
    # status` without --property is locale-formatted and the matcher
    # would silently break under non-en_US locales.
    assert "--property=NTPSynchronized" in text


def test_clock_checks_use_timedatectl_show_not_status():
    """`timedatectl status` is locale-formatted and breaks the matcher
    under non-en_US.UTF-8. `show --property=` is key=value, locale-
    independent. Catch any regression that flips the call shape back."""
    for line_no, line in enumerate(_VALIDATE.read_text().splitlines(), start=1):
        if line.strip() == "- status":
            raise AssertionError(
                f"{_VALIDATE.name}:{line_no} uses `timedatectl status` -- "
                "use `timedatectl show --property=NTPSynchronized` "
                "for locale-stable output"
            )
