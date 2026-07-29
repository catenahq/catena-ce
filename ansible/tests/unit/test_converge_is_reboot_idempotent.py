"""A second converge after a reboot must change nothing.

Both defects here reported `changed` on a converge that had nothing to do,
which is worse than noise: `catena converge` is the operation a self-hoster
re-runs after an edit, and a converge that always reports work done is one
nobody can read for the work it actually did.

  - /etc/hosts. Provider images ship cloud-init `manage_etc_hosts: True`,
    which regenerates the file from a template on EVERY boot. The drop-in that
    was supposed to stop it is not enough on its own: cloud-init merges
    USER-DATA over cloud.cfg.d, so anything that sets manage_etc_hosts at
    create time wins and keeps regenerating. What the template writes --
    `127.0.1.1 <fqdn> <hostname>` -- resolves the name correctly but is not
    byte-identical to the converge's line, so the converge rewrote it every
    run and reported `changed` forever. Fixed by enforcing the property (the
    hostname is mapped) rather than one spelling of the line.
  - apt cache. `update_cache` reports `changed` whenever it actually reaches
    the mirrors, so whether the converge was idempotent depended on how long
    ago the previous one ran. cache_valid_time hid it for reruns minutes
    apart and not for reruns hours apart.

Caught by ce_converge (PLAY RECAP changed=2) on bench
2026-07-28T13-18-43-7606, the first run after its idempotency assertion was
made strict.

Run: uv run pytest tests/unit/test_converge_is_reboot_idempotent.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
COMMON_TASKS = ANSIBLE / "roles" / "common" / "tasks" / "main.yml"

_CLOUD_INIT_DROPIN = "Stop cloud-init regenerating /etc/hosts on every boot"
_CLOUD_INIT_STAT = "Check for a cloud-init config directory"
_HOSTS_CHECK = "Check whether /etc/hosts already maps the hostname to 127.0.1.1"
_HOSTS_LINE = "Ensure /etc/hosts maps the hostname to 127.0.1.1"
_APT_BLOCK = "Refresh apt cache (with proxy-bypass fallback)"


def _tasks() -> list[dict]:
    return yaml.safe_load(COMMON_TASKS.read_text(encoding="utf-8"))


def _names(tasks: list[dict]) -> list[str]:
    return [t.get("name") for t in tasks]


def test_cloud_init_no_longer_owns_etc_hosts():
    """Without this the mapping is rewritten away on every boot."""
    task = next(t for t in _tasks() if t.get("name") == _CLOUD_INIT_DROPIN)
    body = task["ansible.builtin.copy"]
    assert "manage_etc_hosts: false" in body["content"]
    assert body["dest"].startswith("/etc/cloud/cloud.cfg.d/"), (
        "must be a drop-in: editing cloud.cfg itself takes over a file we do "
        "not own, and cloud-init merges cloud.cfg.d/*.cfg over the base"
    )


def test_the_dropin_lands_before_anything_writes_etc_hosts():
    """Ordering is the whole point -- taking the file off cloud-init AFTER
    writing to it leaves the write to be undone by the next boot."""
    names = _names(_tasks())
    assert names.index(_CLOUD_INIT_STAT) < names.index(_CLOUD_INIT_DROPIN), (
        "the stat that guards the drop-in must precede it, or the `when` "
        "references an undefined variable"
    )
    assert names.index(_CLOUD_INIT_DROPIN) < names.index(_HOSTS_LINE)


def test_the_hosts_line_is_only_written_when_the_name_is_unmapped():
    """The drop-in loses to user-data, so the converge has to tolerate a
    cloud-init-written mapping instead of rewriting it into its own spelling
    on every run."""
    tasks = _tasks()
    names = _names(tasks)
    assert names.index(_HOSTS_CHECK) < names.index(_HOSTS_LINE)

    check = next(t for t in tasks if t.get("name") == _HOSTS_CHECK)
    assert check.get("changed_when") is False
    assert check.get("failed_when") is False, (
        "a host with no mapping at all must reach the write, not abort"
    )

    write = next(t for t in tasks if t.get("name") == _HOSTS_LINE)
    assert "_catena_hosts_mapped.rc != 0" in str(write.get("when", "")), (
        "an unconditional write reports changed on every converge of a host "
        "whose /etc/hosts cloud-init regenerates"
    )


def test_refreshing_the_apt_index_is_not_a_change():
    """Reading the mirrors is not a change to this host's configuration, and
    reporting it as one makes idempotency a function of wall-clock time."""
    block = next(t for t in _tasks() if t.get("name") == _APT_BLOCK)
    update = block["block"][0]
    assert update["ansible.builtin.apt"]["update_cache"] is True
    assert update.get("changed_when") is False, (
        "apt update must not report changed; cache_valid_time only narrows "
        "the window in which it happens to be quiet"
    )
