"""Bootstrap/reconcile self-hosted Healthchecks: seed the operator
superuser, the catena project, API keys, the ntfy notification
channel, and the daily backup check. Idempotent -- re-running
reconciles drift without wiping operator-added checks."""
# Managed by Ansible (roles/infrastructure). Do not edit by hand.
#
# Bootstrap/reconcile seed for self-hosted Healthchecks. Runs inside
# the Healthchecks container via `docker exec -i ... python manage.py
# shell <`. Idempotent: every run uses update_or_create + .add() on
# M2M bindings, so re-running only reconciles drift (no wipe of client
# additions).
#
# Per-host values arrive via `docker exec -e KEY=VALUE` flags rendered
# in roles/infrastructure/tasks/healthchecks.yml. The script reads them
# from os.environ; missing vars surface as KeyError so a wiring break
# fails loud rather than silently seeding empty strings.
#
# Seeds:
#   0. Creates the operator superuser + default Project if missing. The
#      upstream image's entrypoint runs migrations only - it does NOT
#      honour SUPERUSER_EMAIL/SUPERUSER_PASSWORD, so a fresh container
#      starts with an empty auth_user table. Without this bootstrap the
#      oauth2-proxy forward-auth hop (X-Forwarded-Email header) has no
#      User row to map onto and the UI 403s for every request.
#   1. Project.api_key_readonly + ping_key + name (pinned to vault).
#   2. Removes Healthchecks's tutorial check + default email channel if
#      present (neither is wanted here).
#   3. ntfy Channel for the operator's topic (update-in-place).
#   4. "Daily backup ping" check (dead-man, 1d timeout + 2h grace).
#   5. Binds ntfy channel to the backup check via .add() (not .set()),
#      so client-added channels survive converges.
#
# Gatus per-endpoint checks (gatus-<slug>) are NOT seeded here: Gatus
# creates them on first failure via `?create=1`, and Healthchecks's
# `Check.assign_all_channels()` auto-attaches every project channel to
# the new check - so the operator's ntfy channel (seeded below) lands
# on every auto-created gatus-* check with zero extra wiring.

import json
import os
from datetime import timedelta
from django.contrib.auth import get_user_model
from hc.accounts.models import Project
from hc.api.models import Channel, Check

_hc_email = os.environ["CATENA_ADMIN_EMAIL"]
_hc_pw = os.environ["CATENA_HC_SUPERUSER_PASSWORD"]
_hc_inventory_hostname = os.environ["CATENA_INVENTORY_HOSTNAME"]
_hc_api_key_readonly = os.environ["CATENA_HC_API_KEY_READONLY"]
_hc_api_key_readwrite = os.environ.get("CATENA_HC_API_KEY_READWRITE", "")
_hc_ping_key = os.environ["CATENA_HC_PING_KEY"]
_hc_ntfy_topic = os.environ["CATENA_NTFY_TOPIC"]
_hc_ntfy_server = os.environ["CATENA_NTFY_SERVER"]

User = get_user_model()

# Bootstrap the superuser. Use username=email so the oauth2-proxy
# forward-auth hop (REMOTE_USER_HEADER=HTTP_X_FORWARDED_EMAIL) can map
# the incoming email to this User. On re-converge we reconcile the
# staff/superuser flags but never touch the password - if the operator
# changed it via /admin/ we don't want to stomp it.
operator = User.objects.filter(username=_hc_email).first()
if operator is None:
    operator = User.objects.create_superuser(
        username=_hc_email, email=_hc_email, password=_hc_pw,
    )
else:
    _dirty = False
    if operator.email != _hc_email:
        operator.email = _hc_email
        _dirty = True
    if not operator.is_superuser:
        operator.is_superuser = True
        _dirty = True
    if not operator.is_staff:
        operator.is_staff = True
        _dirty = True
    if _dirty:
        operator.save(update_fields=["email", "is_superuser", "is_staff"])

# Ensure the operator has a Project to own the seeded checks/channels.
# Project.objects.create() does NOT trigger the signup-flow helpers
# (tutorial check + default email channel), so we don't need to delete
# them below - but the deletes stay as defensive no-ops.
if not Project.objects.filter(owner=operator).exists():
    Project.objects.create(owner=operator, name=_hc_inventory_hostname)

project = Project.objects.filter(owner=operator).first()
if not project:
    raise SystemExit("Healthchecks project bootstrap failed unexpectedly")

project.api_key_readonly = _hc_api_key_readonly
# RW key powers /usr/local/bin/gatus-sync's orphan-pause pass: GET
# /api/v3/checks/ needs readonly, POST /api/v3/checks/<uuid>/pause/ needs
# the full api_key. Empty value (migrate-path) leaves the existing
# project.api_key in place - HC keeps any prior value.
_save_fields = ["api_key_readonly", "ping_key", "name"]
if _hc_api_key_readwrite:
    project.api_key = _hc_api_key_readwrite
    _save_fields.insert(0, "api_key")
project.ping_key = _hc_ping_key
if not project.name:
    project.name = _hc_inventory_hostname
project.save(update_fields=_save_fields)

# Defensive clean-up: Healthchecks's signup flow creates a "My first
# check" tutorial + a default email Channel. Our bootstrap above uses
# create_superuser (no signup flow), so nothing lands here on fresh
# installs - but if an operator ever re-creates the project manually
# through the UI, these deletes reconcile it back. Idempotent.
Check.objects.filter(project=project, name="My first check").delete()
Channel.objects.filter(project=project, kind="email").delete()

ntfy_value = json.dumps({
    "topic": _hc_ntfy_topic,
    "url": _hc_ntfy_server,
    "priority": 3,
    "priority_up": 3,
})
channel, _ = Channel.objects.update_or_create(
    project=project,
    kind="ntfy",
    defaults={"value": ntfy_value, "name": "ntfy ({})".format(_hc_inventory_hostname)},
)

# R24: two backup checks, not one.
#
#   - succeeded: pinged ONLY on a clean run-end. grace=26h means a single
#     missed nightly run goes "late" but stays UP; a SECOND consecutive
#     miss (50h since last success) trips DOWN. This is the "alarm on
#     N=2 consecutive misses" semantic - soft S3/restic transients no
#     longer page on first failure.
#
#   - attempted: pinged on every run start AND on /fail for hard
#     structural failures (pg_dumpall abort, restic config error). Tight
#     grace=2h - operator pages immediately if the host stops attempting
#     backups at all (timer dead, host down) or the wrapper hits an
#     unrecoverable error.
backup_succeeded_check, _ = Check.objects.update_or_create(
    project=project,
    slug="catena-backup-succeeded",
    defaults={
        "name": "Backup succeeded (sliding-window)",
        "desc": (
            "Pinged only when run-backup.sh completes cleanly. Grace=26h "
            "buffers a single transient failure: one miss = LATE, two "
            "consecutive misses = DOWN."
        ),
        "kind": "simple",
        "timeout": timedelta(days=1),
        "grace": timedelta(hours=26),
    },
)
backup_succeeded_check.channel_set.add(channel)

backup_attempted_check, _ = Check.objects.update_or_create(
    project=project,
    slug="catena-backup-attempted",
    defaults={
        "name": "Backup attempted (immediate)",
        "desc": (
            "Pinged on every run start; /fail on hard structural failures "
            "(pg_dumpall abort, restic config error). Tight grace=2h - "
            "alerts immediately if the timer stops firing or the wrapper "
            "hits an unrecoverable error."
        ),
        "kind": "simple",
        "timeout": timedelta(days=1),
        "grace": timedelta(hours=2),
    },
)
backup_attempted_check.channel_set.add(channel)

# R24 migration: the legacy single-check slug `catena-backup` no
# longer receives pings (run-backup.sh now hits the two slugs above).
# Delete it eagerly so the operator doesn't get a one-time "DOWN" page
# from grace expiring on an orphaned check. Historical pings on this
# slug are lost; the operator can rename via /admin/ before the next
# converge if they want to preserve them.
Check.objects.filter(project=project, slug="catena-backup").delete()

# Clean up the legacy aggregate "Service alerts" check from pre-
# per-endpoint deploys. Idempotent no-op once gone. Leaves any
# auto-created gatus-* per-endpoint checks alone.
Check.objects.filter(project=project, slug="gatus-alerts").delete()

# Community edition seeds NO catena-daily umbrella checks: the nightly
# orchestrator chain (cold mirror, verify-cold, managed updates, CVE
# residual, container update) is a Business-edition lane and does not
# run here. The backup checks above stay -- they monitor the manual
# run-backup.sh (or a user's own backup cron); a never-pinged check is
# inert in Healthchecks, so they raise no false alarm until the first
# real ping starts the dead-man clock.
print(
    "OK channel={} succeeded={} attempted={}".format(
        channel.code,
        backup_succeeded_check.code,
        backup_attempted_check.code,
    )
)
