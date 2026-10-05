"""Seed and reconcile self-hosted Healthchecks: the operator superuser, the
project and its API keys, the ntfy notification channel, and the two backup
checks. Re-running reconciles drift and leaves client-added checks and
channels alone."""
# Managed by Ansible (reconcile/roles/infrastructure). Do not edit by hand.
#
# Runs inside the Healthchecks container via `docker exec -i ... python
# manage.py shell <`. Per-host values arrive as `docker exec -e KEY=VALUE`
# flags rendered in reconcile/roles/infrastructure/tasks/healthchecks.yml; a
# missing one raises KeyError, so a wiring break fails loud.
#
# Seeds:
#   0. The operator superuser and Project, when missing. The upstream
#      entrypoint runs migrations only and ignores
#      SUPERUSER_EMAIL/SUPERUSER_PASSWORD, so a fresh container has an empty
#      auth_user table, and the oauth2-proxy hop (X-Forwarded-Email) needs a
#      User row to map onto.
#   1. Project.api_key_readonly, ping_key and name, pinned to the on-box store.
#   2. Catena's ntfy Channel, when NTFY_SERVER and NTFY_TOPIC are both set.
#      It carries a fixed `code`, so a converge updates or removes that one
#      row and never a channel the client added.
#   3. The two backup checks, bound to Catena's channel with .add(), which
#      keeps the client's own bindings.
#
# Gatus creates its per-endpoint checks (gatus-<slug>) on first failure via
# `?create=1`, and Healthchecks's `Check.assign_all_channels()` attaches every
# project channel to the new check, Catena's ntfy channel included.

import json
import os
import uuid
from datetime import timedelta
from django.contrib.auth import get_user_model
from hc.accounts.models import Profile, Project
from hc.api.models import Channel, Check

_hc_email = os.environ["CATENA_ADMIN_EMAIL"]
_hc_pw = os.environ["CATENA_HC_SUPERUSER_PASSWORD"]
_hc_inventory_hostname = os.environ["CATENA_INVENTORY_HOSTNAME"]
_hc_api_key_readonly = os.environ["CATENA_HC_API_KEY_READONLY"]
_hc_api_key_readwrite = os.environ.get("CATENA_HC_API_KEY_READWRITE", "")
_hc_ping_key = os.environ["CATENA_HC_PING_KEY"]
_hc_ntfy_topic = os.environ["CATENA_NTFY_TOPIC"]
_hc_ntfy_server = os.environ["CATENA_NTFY_SERVER"]
_NTFY_CHANNEL_CODE = uuid.uuid5(uuid.NAMESPACE_URL, "catena:healthchecks-seed:ntfy")

User = get_user_model()

# Bootstrap the superuser. Use username=email so the oauth2-proxy
# identity hop (REMOTE_USER_HEADER=HTTP_X_FORWARDED_EMAIL) can map
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

# Follow the browser's light/dark preference by default.
#
# Healthchecks stores the theme per profile, with no global default and no
# env var: hc/accounts/models.py declares `theme` as a nullable CharField,
# and the accounts view accepts exactly "" (light), "dark" and "system".
# A fresh profile is NULL, which renders light whatever the reader's machine
# is set to -- so a panel in dark mode linked out to a monitoring page in
# light mode.
#
# NULL is what makes this safe to set. It means "never chosen", and it is a
# DIFFERENT value from "", which is what the view stores when somebody picks
# Light deliberately. So this fills in a default exactly once and never
# argues with a client who made a choice -- the same rule the Beszel alert
# thresholds follow.
_profile = Profile.objects.for_user(operator)
if _profile.theme is None:
    _profile.theme = "system"
    _profile.save(update_fields=["theme"])

# The operator's Project owns the seeded checks and channel.
# Project.objects.create() skips the signup flow's tutorial check and email
# channel.
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

# The ntfy channel is OPTIONAL, and both halves are required to make one.
# Neither NTFY_SERVER nor NTFY_TOPIC defaults to a value: a default of
# https://ntfy.sh would be public and unauthenticated, where the topic is
# the only access control, so a host nobody configured would push its
# alerts to a server the operator does not run -- the wrong thing to do by
# default. Requiring both also avoids a channel with an empty topic when
# only the server is set: a route that resolves and delivers nowhere, yet
# reads in the UI as configured.
#
# Both blank is a supported end state, not a half-finished install: the checks
# still record every ping and the client attaches their own channel through
# the Healthchecks integrations UI. Clearing the values on a converge removes
# the channel an earlier converge seeded, rather than leaving a stale one
# nobody can see is dead. Both writes select on the fixed code, so the
# client's own ntfy channels stay as they are.
if _hc_ntfy_topic and _hc_ntfy_server:
    ntfy_value = json.dumps({
        "topic": _hc_ntfy_topic,
        "url": _hc_ntfy_server,
        "priority": 3,
        "priority_up": 3,
    })
    channel, _ = Channel.objects.update_or_create(
        project=project,
        code=_NTFY_CHANNEL_CODE,
        defaults={
            "kind": "ntfy",
            "value": ntfy_value,
            "name": "ntfy ({})".format(_hc_inventory_hostname),
        },
    )
else:
    channel = None
    removed, _ = Channel.objects.filter(
        project=project, code=_NTFY_CHANNEL_CODE,
    ).delete()
    print(
        "healthchecks-seed: no notification channel configured "
        "(NTFY_SERVER and NTFY_TOPIC must both be set); checks will record "
        "pings but nothing will be pushed. Removed {} stale ntfy channel(s)."
        .format(removed)
    )

# Two backup checks:
#
#   - succeeded: pinged only on a clean run end. grace=26h: one missed
#     nightly run goes "late" but stays UP, and a second consecutive miss
#     (50h since the last success) trips DOWN, so a soft S3/restic transient
#     does not page.
#
#   - attempted: pinged on every run start, and on /fail for hard
#     structural failures (pg_dumpall abort, restic config error). grace=2h
#     pages as soon as the host stops attempting backups (timer dead, host
#     down) or the wrapper hits an unrecoverable error.
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
if channel is not None:
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
if channel is not None:
    backup_attempted_check.channel_set.add(channel)

# A check nobody has pinged stays inert in Healthchecks: its dead-man clock
# starts at the first ping from run-backup.sh.
print(
    "OK channel={} succeeded={} attempted={}".format(
        channel.code if channel is not None else "none",
        backup_succeeded_check.code,
        backup_attempted_check.code,
    )
)
