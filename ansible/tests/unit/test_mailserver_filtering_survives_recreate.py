"""rspamd filtering comes back with a new dms container, not with the next converge.

/etc/rspamd is container filesystem. A new dms task (node reboot, swarm
reschedule, image update, restore) starts from the image defaults: antivirus
off, stock thresholds. docker-mailserver copies only rspamd/override.d from
the mail-config volume at start, never local.d, so the drop-ins the converge
staged there were not re-applied until a converge noticed the drift.

Bench run 2026-09-25T04-10-34-7e65 caught it: after an anchor rewind the first
converge on a slot host reloaded rspamd, the one change ce_converge's settle
stage does not allow. docker-mailserver runs /tmp/docker-mailserver/
user-patches.sh once per new container, after its rspamd setup and before
rspamd starts, which is where the drop-ins are re-applied now.

Run: uv run pytest tests/unit/test_mailserver_filtering_survives_recreate.py
"""
from __future__ import annotations

from pathlib import Path

import jinja2
import yaml

TASKS = (Path(__file__).resolve().parents[2]
         / "reconcile" / "roles" / "infrastructure" / "tasks")


def _block() -> list[dict]:
    tasks = yaml.safe_load((TASKS / "mailserver_filtering.yml").read_text())
    return next(t for t in tasks if isinstance(t, dict) and "block" in t)["block"]


def _task(prefix: str) -> dict:
    return next(t for t in _block() if t["name"].startswith(prefix))


def test_the_hook_copies_exactly_the_staged_drop_ins():
    content = _task("Mailserver filtering: render the dms start hook")[
        "ansible.builtin.copy"]["content"]
    confs = [{"name": "antivirus.conf"}, {"name": "rbl.conf"}]
    rendered = jinja2.Template(content).render(_ms_rspamd_confs=confs)
    cps = [ln for ln in rendered.splitlines() if ln.startswith("cp ")]
    assert cps == [
        "cp /tmp/docker-mailserver/rspamd/local.d/antivirus.conf "
        "/etc/rspamd/local.d/antivirus.conf",
        "cp /tmp/docker-mailserver/rspamd/local.d/rbl.conf "
        "/etc/rspamd/local.d/rbl.conf",
    ]
    assert rendered.startswith("#!/bin/bash\n")


def test_the_hook_lands_where_docker_mailserver_runs_it():
    argv = _task("Mailserver filtering: stage the start hook")[
        "ansible.builtin.command"]["argv"]
    assert argv[:2] == ["/usr/local/bin/catena-dms-exec", "cp"]
    assert argv[-1] == "/tmp/docker-mailserver/user-patches.sh"


def test_the_hook_is_staged_after_the_drop_ins_it_copies():
    names = [t["name"] for t in _block()]
    staged = names.index(
        "Mailserver filtering: stage drop-ins into the mail-config volume")
    hook = next(i for i, n in enumerate(names)
                if n.startswith("Mailserver filtering: stage the start hook"))
    assert staged < hook
