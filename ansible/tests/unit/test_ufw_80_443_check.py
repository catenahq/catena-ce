"""The "nothing opens 80/443" validate check, against real ufw output.

The architecture invariant is that 80 and 443 reach Traefik through the
Cloudflare tunnel and are never open on the host. validate.yml enforces it by
grepping `ufw status`, and the pattern it used was wrong in two directions at
once:

  it matched too much   `(80|443)/tcp` is a substring match against the whole
                        row, so ANY port ending in 80 or 443 tripped it. Bench
                        050b failed on `18080/tcp DENY` -- gatus' loopback
                        port, denied off-box by the scope=loopback registry
                        declaration. A correctly-guarded port failed the
                        guard.

  it matched the wrong  the assertion is "nothing OPENS these", but it fired
  thing                 on any matching row regardless of action, so an
                        explicit DENY on 80 -- exactly what you would want to
                        find -- would have failed the validate too.

The pattern is read out of the task file rather than restated here, so the
test cannot pass against a pattern the converge does not use.

Run: uv run pytest tests/unit/test_ufw_80_443_check.py
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

VALIDATE = (
    Path(__file__).resolve().parents[2]
    / "reconcile" / "roles" / "infrastructure" / "tasks" / "validate.yml"
)

# A realistic `ufw status` body. The header rows are present because the
# pattern has to survive them.
_HEADER = (
    "Status: active\n"
    "\n"
    "To                         Action      From\n"
    "--                         ------      ----\n"
)


def _pattern() -> str:
    """The grep -E pattern the converge actually runs."""
    tasks = yaml.safe_load(VALIDATE.read_text(encoding="utf-8"))
    task = next(
        t for t in tasks
        if "ufw does not allow public 80/443" in (t.get("name") or "")
    )
    shell = task["ansible.builtin.shell"]
    m = re.search(r"grep -E '([^']+)'", shell)
    assert m, f"could not read the grep pattern out of: {shell!r}"
    return m.group(1)


def _hits(body: str) -> list[str]:
    """Lines grep -E would emit for the task's pattern."""
    rx = re.compile(_pattern())
    return [ln for ln in body.splitlines() if rx.search(ln)]


# --- what must FAIL the validate ------------------------------------------

@pytest.mark.parametrize("row", [
    "80/tcp                     ALLOW       Anywhere",
    "443/tcp                    ALLOW       Anywhere",
    "80/tcp (v6)                ALLOW       Anywhere (v6)",
    "443/tcp (v6)               ALLOW       Anywhere (v6)",
])
def test_an_open_80_or_443_is_caught(row):
    assert _hits(_HEADER + row + "\n") == [row]


# --- what must NOT fail the validate --------------------------------------

@pytest.mark.parametrize("row", [
    # The regression: a high port whose digits end in 80 or 443.
    "18080/tcp                  DENY        Anywhere                   # catena-ports:gatus",
    "18080/tcp (v6)             DENY        Anywhere (v6)              # catena-ports:gatus",
    "8443/tcp                   ALLOW       Anywhere",
    "10443/tcp                  ALLOW       Anywhere",
    # An explicit deny on the real ports is the desired state, not a
    # violation.
    "80/tcp                     DENY        Anywhere",
    "443/tcp                    DENY        Anywhere",
    # Other protocols and unrelated services.
    "5349/tcp                   ALLOW       Anywhere                   # catena-ports:coturn",
    "22/tcp                     ALLOW       Anywhere",
])
def test_unrelated_or_denied_rows_are_ignored(row):
    assert _hits(_HEADER + row + "\n") == []


def test_a_clean_firewall_is_clean():
    assert _hits(_HEADER) == []


def test_catches_the_open_port_among_the_noise():
    """The rows that regressed and the row that matters, together -- a
    pattern that is merely anchored but action-blind would return three
    hits here."""
    body = _HEADER + "\n".join([
        "18080/tcp                  DENY        Anywhere                   # catena-ports:gatus",
        "18099/tcp                  DENY        Anywhere                   # catena-ports:healthchecks",
        "80/tcp                     ALLOW       Anywhere",
        "5349/tcp                   ALLOW       Anywhere                   # catena-ports:coturn",
    ]) + "\n"
    assert _hits(body) == ["80/tcp                     ALLOW       Anywhere"]
