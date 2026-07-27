"""Scheduling and retention each have exactly one owner.

Every defect this replaced was a second source of truth:

  - retention was templated into backup.env AND read from flat
    config.BACKUP_KEEP_* keys at runtime -- and backup.env is written
    only-if-absent, so on any host that already had it the converge's copy
    was dead while the store's copy was live;
  - the tier filter existed twice, in this repo and in ops, and the ops test
    suite imported the ops copy that no playbook used;
  - timer enable would have been owned by both this role and
    `catena-schedule apply`, which disagree the moment somebody turns a lane
    off in the panel and the next converge turns it back on.

So these are structural assertions, not behavioural ones. They fail when a
second writer appears.

Run: uv run pytest tests/unit/test_schedule_single_owner.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
BACKUP_INSTALL = ANSIBLE / "roles" / "backup" / "tasks" / "install.yml"
BACKUP_ENV = ANSIBLE / "roles" / "backup" / "templates" / "backup.env.j2"
BACKUP_DEFAULTS = ANSIBLE / "roles" / "backup" / "defaults" / "main.yml"
RUN_BACKUP = ANSIBLE / "scripts" / "run-backup.sh"
ADMIN_HOST = ANSIBLE / "roles" / "catena-admin" / "tasks" / "host.yml"
ADMIN_DEPLOY = ANSIBLE / "roles" / "catena-admin" / "tasks" / "deploy.yml"
DAILY_ENV = ANSIBLE / "roles" / "catena-admin" / "templates" / "daily.env.j2"

KEEP_KEYS = (
    "BACKUP_KEEP_LAST", "BACKUP_KEEP_HOURLY", "BACKUP_KEEP_DAILY",
    "BACKUP_KEEP_WEEKLY", "BACKUP_KEEP_MONTHLY",
)


def _code(path: Path) -> str:
    """The file with comment lines stripped, so a comment naming an
    anti-pattern in order to rule it out does not read as the thing."""
    return "\n".join(
        line for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )


# ─── retention has one writer and one reader ───────────────────────────


def test_retention_is_not_templated_into_backup_env():
    # backup.env is written only-if-absent to protect a rotated credential,
    # so anything mutable in it is unreachable on every existing host.
    body = _code(BACKUP_ENV)
    for key in KEEP_KEYS:
        assert key not in body, (
            f"{key} is templated into backup.env, which is written "
            "only-if-absent -- the value would never reach an existing host"
        )


def test_this_repo_sets_no_retention_default():
    body = _code(BACKUP_DEFAULTS)
    for gone in ("backup_retention_policy", "backup_retention_defaults"):
        assert gone not in body, (
            f"{gone} makes this repo a second writer of a value the panel owns"
        )


def test_run_backup_reads_retention_from_the_one_file():
    body = _code(RUN_BACKUP)
    assert "BACKUP_RETENTION_ENV" in body
    assert "/etc/catena/backup-retention.env" in body
    # And not from the store's flat keys any more.
    assert "_store_get config BACKUP_KEEP" not in body, (
        "the flat config.BACKUP_KEEP_* keys are a second source of retention"
    )


def test_an_empty_retention_policy_refuses_to_prune():
    # Every bucket zero asks restic to forget every snapshot in the
    # repository. Reaching the prune with nothing to keep means the env file
    # was hand-edited; running it anyway destroys the backups on a typo.
    body = _code(RUN_BACKUP)
    assert 'if [ -z "$keep_args" ]; then' in body
    assert "exit 5" in body


# ─── the timer has one owner ───────────────────────────────────────────


def test_the_backup_role_installs_the_timer_and_enables_nothing():
    tasks = yaml.safe_load(BACKUP_INSTALL.read_text())
    for t in tasks:
        svc = t.get("ansible.builtin.systemd_service") or {}
        name = str(svc.get("name", ""))
        if ".timer" in name:
            assert not svc.get("enabled"), (
                f"{t.get('name')} enables {name}; catena-schedule owns that, "
                "and two owners disagree the moment a lane is turned off"
            )


def test_the_converge_applies_the_stored_schedule():
    tasks = yaml.safe_load(ADMIN_DEPLOY.read_text())
    applies = [
        t for t in tasks
        if "catena_schedule_bin" in str(t.get("ansible.builtin.command", ""))
    ]
    assert len(applies) == 1, "exactly one task should apply the schedule"
    assert "apply" in str(applies[0]["ansible.builtin.command"]["argv"])


# ─── the daily chain can start at all ──────────────────────────────────


def test_the_daily_env_file_is_rendered_every_converge():
    # catena-daily.service declares EnvironmentFile for this path with no
    # leading dash. A missing file fails the unit outright, which is how the
    # scheduled chain came to be unable to start on any real host.
    tasks = yaml.safe_load(ADMIN_HOST.read_text())
    renders = [
        t for t in tasks
        if str(t.get("ansible.builtin.template", {}).get("src", "")) == "daily.env.j2"
    ]
    assert len(renders) == 1, "daily.env must be rendered exactly once"
    # Unconditional: it carries no secrets, so there is nothing for a
    # re-render to clobber, and ordering on a first converge is one less
    # thing to get wrong.
    assert "when" not in renders[0], "daily.env must not be conditional"


def test_the_daily_env_carries_no_secret():
    body = DAILY_ENV.read_text(encoding="utf-8")
    for leak in ("vault_", "AWS_SECRET", "RESTIC_PASSWORD", "_password"):
        assert leak not in body, (
            f"{leak!r} in daily.env: it is mode 0644 and the engines read "
            "credentials from the config store"
        )


def test_the_daily_env_does_not_carry_a_schedule():
    # Which timers run and when is catena-schedule's, from the config store.
    body = _code(DAILY_ENV)
    assert "OnCalendar" not in body
    assert "DAILY_TIMER_ONCALENDAR" not in body
