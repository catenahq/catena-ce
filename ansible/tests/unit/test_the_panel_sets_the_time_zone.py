"""The server's time zone and locale are set in the panel and applied by the
host itself.

They are store keys with a panel field, so a reconcile role applies them: the
panel's own converge has to be able to act on what the panel saved. A value
typed there must never fail that converge.

Run: uv run pytest tests/unit/test_the_panel_sets_the_time_zone.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
ROLE = ANSIBLE / "reconcile" / "roles" / "host_maintenance"
KNOBS = ANSIBLE / "helpers" / "knobs.json"


def _knob(key: str) -> dict:
    import json

    doc = json.loads(KNOBS.read_text(encoding="utf-8"))
    return next(e for e in doc["config"] if e["key"] == key)


def test_both_are_panel_fields_with_no_env_line():
    for key, var in (("COMMON_TIMEZONE", "cfg_common_timezone"),
                     ("COMMON_LOCALE", "cfg_common_locale")):
        knob = _knob(key)
        assert (knob["residence"], knob["var"]) == ("store", var)
        assert knob.get("panel") and "env" not in knob, key


def test_a_reconcile_role_applies_them_from_the_store():
    boundary = yaml.safe_load((ANSIBLE / "boundary.yml").read_text(encoding="utf-8"))
    assert "host_maintenance" in boundary["reconcile_roles"]
    defaults = (ROLE / "defaults" / "main.yml").read_text(encoding="utf-8")
    assert "cfg_common_timezone" in defaults and "cfg_common_locale" in defaults
    for path in (ANSIBLE / "playbooks" / "group_vars" / "all" / "main.yml",
                 ANSIBLE / "bootstrap" / "roles" / "common" / "tasks" / "main.yml"):
        body = path.read_text(encoding="utf-8")
        assert "COMMON_TIMEZONE" not in body and "common_timezone" not in body, path


def test_a_zone_the_host_does_not_know_is_not_applied():
    tasks = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text(encoding="utf-8"))
    setter = next(t for t in tasks if t.get("name") == "Host: set the time zone")
    assert "_host_tz_file.stat.isreg" in " ".join(setter["when"])
    report = next(t for t in tasks
                  if t.get("name") == "Host: the time zone is not one this host knows")
    assert "catena-admin > Settings" in report["ansible.builtin.debug"]["msg"]
