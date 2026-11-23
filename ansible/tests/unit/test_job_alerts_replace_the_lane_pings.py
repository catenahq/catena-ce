"""The scheduled jobs report to the alert hub, so no template renders a ping URL.

The properties held here:

- no env template renders a `*_HEALTHCHECK_*` key, and no role default builds a
  ping URL;
- the heartbeat env names the alert intake's loopback port and no Healthchecks
  key;
- the alert switches of the scheduled jobs are declared like every other alert
  switch: a store choice, off by default, with no variable and no label.

Run: uv run pytest tests/unit/test_job_alerts_replace_the_lane_pings.py
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
ROLES = ANSIBLE / "reconcile" / "roles"
INFRA = ROLES / "infrastructure"

PING_KEY = re.compile(r"^[A-Z0-9_]*HEALTHCHECK[A-Z0-9_]*=", re.MULTILINE)
PING_URL = re.compile(r"/ping/|healthchecks_ping_base")

JOB_ALERTS = ("BACKUP_FAILED", "RESTORE_TEST_FAILED", "OFFSITE_FAILED", "REPO_CHECK_FAILED", "JOB_LATE")


def test_no_template_renders_a_healthcheck_key():
    templates = sorted(ROLES.rglob("*.j2"))
    assert templates
    for path in templates:
        assert not PING_KEY.search(path.read_text()), f"{path.relative_to(ANSIBLE)} renders a HEALTHCHECK key"


def test_no_role_default_builds_a_ping_url():
    for path in sorted(ROLES.glob("*/defaults/main.yml")):
        assert not PING_URL.search(path.read_text()), f"{path.relative_to(ANSIBLE)} builds a ping URL"


def test_the_heartbeat_probes_the_intake_not_healthchecks():
    env = (INFRA / "templates" / "heartbeat.env.j2").read_text()
    assert "ALERT_INTAKE_LOOPBACK_PORT={{ alert_intake_host_localhost_port }}" in env
    for gone in ("HEALTHCHECKS_LOOPBACK_PORT", "HC_PING_KEY", "HEARTBEAT_CHECK_SLUG"):
        assert gone not in env


def test_the_job_alert_switches_are_declared_off_by_default():
    knobs = {k["key"]: k for k in yaml.safe_load((ANSIBLE / "helpers" / "knobs.yml").read_text())["config"]}
    for base in ("ALERT_" + name for name in JOB_ALERTS):
        for suffix in ("", "_CLEAR", "_REMIND"):
            knob = knobs[base + suffix]
            assert knob["residence"] == "store", knob
            assert "var" not in knob and "label" not in knob and "help" not in knob, knob
            panel = knob["panel"]
            assert panel["group"] == "alerts" and panel["kind"] == "choice", knob
            assert panel["options"] == ["off", "on"] and "default" not in panel, knob
