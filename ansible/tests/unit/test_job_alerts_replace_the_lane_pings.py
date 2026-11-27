"""The scheduled jobs report to the alert hub.

The properties held here:

- the heartbeat env names the alert intake's loopback port and no ping key or
  check slug;
- the alert switches of the scheduled jobs are declared like every other alert
  switch: a store choice, off by default, with no variable and no label.

Run: uv run pytest tests/unit/test_job_alerts_replace_the_lane_pings.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
INFRA = ANSIBLE / "reconcile" / "roles" / "infrastructure"

JOB_ALERTS = ("BACKUP_FAILED", "RESTORE_TEST_FAILED", "OFFSITE_FAILED", "REPO_CHECK_FAILED", "JOB_LATE")


def test_the_heartbeat_probes_the_intake():
    env = (INFRA / "templates" / "heartbeat.env.j2").read_text()
    assert "ALERT_INTAKE_LOOPBACK_PORT={{ alert_intake_host_localhost_port }}" in env
    for gone in ("HC_PING_KEY", "HEARTBEAT_CHECK_SLUG"):
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
