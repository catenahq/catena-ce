"""The sshd hardening must be APPLIED, not merely written.

Every directive bootstrap/roles/common sets ships as a drop-in under
/etc/ssh/sshd_config.d/. sshd reads that directory only when the main
config includes it. Debian 12 and Ubuntu ship the Include, but a provider
image or a cloud-init variant can replace sshd_config with something
minimal that does not -- and the resulting failure is silent, fails OPEN
(password auth and root login stay enabled, AllowUsers never applies), and
passes any file-content validation that reads the file we wrote rather than
what sshd resolved.

Observed on a live bench host: /etc/ssh/sshd_config held a single line,
`PasswordAuthentication yes`, and `sshd -T` reported
`passwordauthentication yes` / `permitrootlogin without-password` / no
`allowusers` while both drop-ins sat on disk saying the opposite.

Run: uv run pytest tests/unit/test_sshd_dropin_is_effective.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

_ANSIBLE = Path(__file__).resolve().parents[2]
COMMON_MAIN = _ANSIBLE / "bootstrap" / "roles" / "common" / "tasks" / "main.yml"
COMMON_VALIDATE = _ANSIBLE / "bootstrap" / "roles" / "common" / "tasks" / "validate.yml"

INCLUDE_LINE = "Include /etc/ssh/sshd_config.d/*.conf"


def _tasks(path: Path) -> list[dict]:
    return yaml.safe_load(path.read_text()) or []


def _named(tasks: list[dict], needle: str) -> dict:
    for t in tasks:
        if needle in (t.get("name") or ""):
            return t
    raise AssertionError(f"no task whose name contains {needle!r}")


def test_the_converge_establishes_the_include() -> None:
    task = _named(_tasks(COMMON_MAIN), "include the drop-in directory")
    line = task["ansible.builtin.lineinfile"]
    assert line["path"] == "/etc/ssh/sshd_config"
    assert line["line"] == INCLUDE_LINE


def test_the_include_goes_first_not_last() -> None:
    """sshd takes the FIRST value it sees for most keywords. An Include
    appended below a directive the drop-in means to override parses fine and
    changes nothing, which is the same silent no-op as having no Include."""
    line = _named(_tasks(COMMON_MAIN), "include the drop-in directory")["ansible.builtin.lineinfile"]
    assert line.get("insertbefore") == "BOF", (
        "the Include must be prepended; appended, the drop-in loses every "
        "keyword the main config already sets"
    )


def test_the_include_edit_is_validated_before_it_lands() -> None:
    """A malformed sshd_config locks everyone out of a box whose only other
    path in is the provider console."""
    line = _named(_tasks(COMMON_MAIN), "include the drop-in directory")["ansible.builtin.lineinfile"]
    assert "sshd -t" in line.get("validate", "")


def test_the_include_is_added_only_when_missing() -> None:
    task = _named(_tasks(COMMON_MAIN), "include the drop-in directory")
    assert "_sshd_include.rc != 0" in str(task.get("when", ""))


def test_validate_asserts_the_effective_config_not_just_the_file() -> None:
    """The file-content assertion cannot distinguish 'written and applied'
    from 'written and ignored'. Only sshd's own resolved view can."""
    tasks = _tasks(COMMON_VALIDATE)
    probe = _named(tasks, "sshd effective config")
    assert probe["ansible.builtin.command"]["cmd"] == "sshd -T"

    check = _named(tasks, "what sshd actually applies")
    that = " ".join(check["ansible.builtin.assert"]["that"])
    for directive in (
        "permitrootlogin no",
        "passwordauthentication no",
        "pubkeyauthentication yes",
        "allowusers",
    ):
        assert directive in that, f"{directive} not asserted against sshd -T"
