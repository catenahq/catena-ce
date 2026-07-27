"""Scheduling, retention, and every host config file have exactly one owner.

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


# ─── the other lanes can start at all ──────────────────────────────────
#
# daily.env was not the only one. catena-auto-update{,@,-resume}.service and
# catena-stack-update-managed.service declare their EnvironmentFile with no
# leading dash too, and the container engine's --specs-file defaults to a path
# nothing wrote. All four were missing on every real host for the same reason,
# and stayed invisible for the same reason: the bench shipped its own copies.

LANE_CONFIG_TEMPLATES = (
    "auto-update.env.j2",
    "stack-update.env.j2",
    "managed-services.json.j2",
)


def test_every_lane_config_file_is_rendered_exactly_once():
    tasks = yaml.safe_load(ADMIN_HOST.read_text())
    for src in LANE_CONFIG_TEMPLATES:
        renders = [
            t for t in tasks
            if str(t.get("ansible.builtin.template", {}).get("src", "")) == src
        ]
        assert len(renders) == 1, f"{src} must be rendered exactly once"
        assert "when" not in renders[0], (
            f"{src} must not be conditional: a unit whose EnvironmentFile is "
            "sometimes absent is a unit that sometimes cannot start"
        )


def test_the_secret_bearing_lane_config_is_not_world_readable():
    # auto-update.env carries the provider credentials when a provider is
    # configured; stack-update.env carries the Portainer API key.
    tasks = yaml.safe_load(ADMIN_HOST.read_text())
    for src in ("auto-update.env.j2", "stack-update.env.j2"):
        tpl = next(
            t["ansible.builtin.template"] for t in tasks
            if str(t.get("ansible.builtin.template", {}).get("src", "")) == src
        )
        assert str(tpl["mode"]) == "0600", f"{src} must be 0600"


def test_the_worm_env_is_rendered_every_converge_not_only_if_absent():
    # backup.env next door is only-if-absent so a converge never clobbers a
    # rotated credential. The WORM coordinates have to be able to change --
    # an operator adding a cold tier to an existing host would otherwise write
    # into a file nothing rewrites, which is the retention bug again.
    tasks = yaml.safe_load(BACKUP_INSTALL.read_text())
    renders = [
        t for t in tasks
        if str(t.get("ansible.builtin.template", {}).get("src", ""))
        == "backup-worm.env.j2"
    ]
    assert len(renders) == 1, "backup-worm.env must be rendered exactly once"
    assert "when" not in renders[0], (
        "backup-worm.env must be unconditional: every value in it is "
        "legitimately blank, and blank means the mirror skips"
    )
    assert renders[0].get("no_log") is True, "it carries the cold-tier keys"


def test_the_worm_env_is_the_only_place_the_worm_keys_are_written():
    # They were in neither file before this, which is why the cold mirror
    # reported "WORM unconfigured" on every host it ever ran on.
    assert "BACKUP_WORM_REPO" not in _code(BACKUP_ENV), (
        "the WORM coordinates cannot live in backup.env: it is written "
        "only-if-absent and they have to be able to change"
    )
    worm_env = ANSIBLE / "roles" / "backup" / "templates" / "backup-worm.env.j2"
    body = _code(worm_env)
    for key in ("BACKUP_WORM_REPO", "BACKUP_WORM_ACCESS_KEY_ID",
                "NEXTCLOUD_WORM_REPO"):
        assert key in body


def test_the_managed_lane_needs_no_copy_of_how_traefik_was_built():
    # This used to assert that traefik_run_argv had exactly ONE definition,
    # shared between roles/traefik and the managed-bump lane's spec file. The
    # lane recreated catena-traefik on a version bump, so it had to reproduce
    # the container the converge would have made, and the two copies drifted
    # once -- the lane relaunched traefik with no config mounts at all.
    #
    # The swarm conversion removed the reason for the copy rather than keeping
    # the copies in step: `docker service update --image` changes only the
    # image, so the lane never needs to know how the service was built. What
    # has to hold now is that NO argv came back.
    specs = _code(
        ANSIBLE / "roles" / "catena-admin" / "templates"
        / "managed-services.json.j2"
    )
    # Assert on the JSON keys, not bare words: the Jinja {# #} header explains
    # why run_argv and infra-container are gone, and _code only strips `#`
    # comment LINES, so the prose would otherwise read as the thing.
    assert '"run_argv"' not in specs, (
        "the managed lane is carrying a copy of how a service was built again; "
        "an infra-swarm spec needs only the service name"
    )
    assert '"kind": "infra-container"' not in specs, (
        "infra-container is gone -- the engine branch that consumed it was "
        "deleted with the traefik swarm conversion"
    )
    assert '"kind": "infra-swarm"' in specs

    traefik_tasks = _code(ANSIBLE / "roles" / "traefik" / "tasks" / "main.yml")
    assert "docker run" not in traefik_tasks, (
        "catena-traefik is a swarm service; a `docker run` here means the "
        "plain-container shape came back, and with it the overlay-ID staleness "
        "that stranded ingress on every daemon restart"
    )
