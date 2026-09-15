"""Scheduling, retention, and every host config file have exactly one owner.

Every defect in this family is a second source of truth:

  - retention templated into backup.env AND read from flat config.BACKUP_KEEP_*
    keys at runtime -- and backup.env is written only-if-absent, so on a host
    that already has it one copy is live and the other is dead;
  - the tier filter written twice, in this repo and in ops, with the ops test
    suite importing the ops copy that no playbook runs;
  - timer enable owned by both this role and `catena-schedule apply`, which
    disagree the moment somebody turns a lane off in the panel and the next
    converge turns it back on.

So these are structural assertions, not behavioural ones. They fail when a
second writer appears.

Run: uv run pytest tests/unit/test_schedule_single_owner.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
BACKUP_INSTALL = ANSIBLE / "reconcile" / "roles" / "backup" / "tasks" / "install.yml"
BACKUP_ENV = ANSIBLE / "reconcile" / "roles" / "backup" / "templates" / "backup.env.j2"
BACKUP_DEFAULTS = ANSIBLE / "reconcile" / "roles" / "backup" / "defaults" / "main.yml"
ADMIN_HOST = ANSIBLE / "bootstrap" / "roles" / "catena_admin_host" / "tasks" / "main.yml"
ADMIN_DEPLOY = ANSIBLE / "reconcile" / "roles" / "catena-admin" / "tasks" / "deploy.yml"
DAILY_ENV = ANSIBLE / "bootstrap" / "roles" / "catena_admin_host" / "templates" / "daily.env.j2"
GROUP_VARS = ANSIBLE / "playbooks" / "group_vars" / "all" / "main.yml"
ONBOX_CONFIG = ANSIBLE / "helpers" / "onbox_config.py"

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
    # Both files, not just the role default. The dict was removed from
    # reconcile/roles/backup/defaults and reappeared in group_vars/all/main.yml, where
    # this assertion could not see it -- and it stayed there, read by no role,
    # template or playbook, so an operator could set BACKUP_KEEP_DAILY=30 in
    # .env, converge clean, and keep 7.
    for path in (BACKUP_DEFAULTS, GROUP_VARS):
        body = _code(path)
        for gone in ("backup_retention_policy", "backup_retention_defaults"):
            assert gone not in body, (
                f"{gone} is back in {path.name}: it makes this repo a second "
                "writer of a value the panel owns, and the last copy was not a "
                "writer at all -- nothing read it"
            )


def test_no_retention_key_is_seeded_into_the_store():
    # BOOTSTRAP_CONFIG is what the inventory may put in the store. Three of the
    # keep keys were in it, described as seeding the store -- into flat config
    # keys the wrapper stopped reading when retention got its single owner. A
    # value stored and never read is indistinguishable, from the operator's
    # side, from one that took effect.
    body = _code(ONBOX_CONFIG)
    for key in KEEP_KEYS:
        assert f'"{key}"' not in body, (
            f"{key} is declared in onbox_config.py again; retention reaches the "
            "store as the backup_retention object, written by catena-schedule"
        )


# The READER half of the retention contract -- what the wrapper sources, and
# that it refuses to prune on an emptied policy while still finishing clean on
# a never-written one -- moved to catena-admin
# payload/lanes/backup_run_retention_owner_test.go with the wrapper. This file
# keeps the WRITER half, which is what this repo owns.


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
    # Gated on the binary being installed, NOT on this converge having
    # deployed the shell. It carried that gate by copy-paste once, and a host
    # whose payload arrived any other way (bench, restore, migrate) converged
    # with no backup-retention.env for run-backup.sh to source.
    gate = str(applies[0].get("when", ""))
    assert "catena_admin_deploy_from_converge" not in gate
    assert "portainer_api_key" not in gate
    assert "stat.exists" in gate


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
# daily.env is not the only one. catena-auto-update{,@,-resume}.service and
# catena-stack-update-managed.service declare their EnvironmentFile with no
# leading dash too, and the container engine's --specs-file defaults to a path
# nothing writes. All four go missing on a real host for the same reason, and
# stay invisible for the same reason: the bench ships its own copies.

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


def test_the_offsite_env_is_rendered_every_converge_not_only_if_absent():
    # backup.env next door is only-if-absent so a converge never clobbers a
    # rotated credential. The lane's dead-man endpoints have to be able to
    # change -- an operator pointing them off-host on an existing host would
    # otherwise write into a file nothing rewrites, the retention bug again.
    tasks = yaml.safe_load(BACKUP_INSTALL.read_text())
    renders = [
        t for t in tasks
        if str(t.get("ansible.builtin.template", {}).get("src", ""))
        == "offsite.env.j2"
    ]
    assert len(renders) == 1, "offsite.env must be rendered exactly once"
    assert "when" not in renders[0], (
        "offsite.env must be unconditional: both values in it are "
        "legitimately blank, and blank means the lane pings nothing"
    )


def test_which_buckets_get_copied_is_not_a_converge_input():
    # Nine converge keys across backup-worm.env and the settings schema can
    # describe exactly two copies, named after the buckets they happen to point
    # at. The list lives in /etc/catena/config.json instead, which catena-admin
    # writes and the lane reads straight off disk -- so a client adding a copy
    # does not need a converge, and no key here can go
    # stale against it.
    offsite_env = ANSIBLE / "reconcile" / "roles" / "backup" / "templates" / "offsite.env.j2"
    body = _code(offsite_env)
    for key in ("OFFSITE_HEALTHCHECK_URL", "OFFSITE_HEALTHCHECK_ATTEMPTED_URL"):
        assert key in body
    for gone in ("BACKUP_WORM_REPO", "BACKUP_WORM_ACCESS_KEY_ID",
                 "NEXTCLOUD_WORM_REPO", "NEXTCLOUD_LIVE_REPO"):
        assert gone not in body, (
            f"{gone} is back in a converge-rendered file; the copy list has "
            "one owner and it is the config store"
        )
        assert gone not in _code(BACKUP_ENV)


def test_the_managed_lane_needs_no_copy_of_how_traefik_was_built():
    # The lane bumps catena-traefik with `docker service update --image`, so
    # it never needs to reproduce how the converge built the service. An argv
    # in the spec file would be a second definition of the role's launch
    # spec, free to drift from it -- and a lane running a drifted copy
    # relaunches traefik with the wrong mounts.
    specs = _code(
        ANSIBLE / "bootstrap" / "roles" / "catena_admin_host" / "templates"
        / "managed-services.json.j2"
    )
    # Assert on the JSON keys, not bare words: the Jinja {# #} header names
    # both terms in prose, and _code only strips `#` comment LINES, so a bare
    # substring check would match the explanation instead of the data.
    assert '"run_argv"' not in specs, (
        "the managed lane is carrying a copy of how a service was built again; "
        "an infra-swarm spec needs only the service name"
    )
    assert '"kind": "infra-container"' not in specs, (
        "infra-container has no engine branch to consume it; infra services "
        "are bumped as swarm services"
    )
    assert '"kind": "infra-swarm"' in specs

    traefik_tasks = _code(ANSIBLE / "reconcile" / "roles" / "traefik" / "tasks" / "main.yml")
    assert "docker run" not in traefik_tasks, (
        "catena-traefik must stay a swarm service; a plain container attaches "
        "to catena-network by ID, so an overlay rebuild strands it with "
        "ingress down"
    )
