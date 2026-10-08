"""Seed and reconcile self-hosted Healthchecks: the operator superuser, the
project and its API keys, and the notification channels (the admin's email and
ntfy). It creates no check. Re-running reconciles drift and leaves
client-added checks and channels alone."""
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
#   2. Catena's two channels, through _ensure_channel: the admin's email while
#      outgoing mail is on, ntfy while NTFY_SERVER and NTFY_TOPIC are both
#      set. Each carries a fixed `code`, so a converge updates or removes that
#      one row and never a channel the client added. A channel is attached to
#      every check when it is created, and never again, so a client who
#      detaches it from a check keeps that choice.
#
# The checks come from elsewhere, each with every project channel attached:
# catena-admin's catena-schedule creates the scheduled lanes' checks through
# the API (`channels: "*"`), and keeps them on the lanes' schedule; Gatus, the
# Beszel shim and the host watchdogs create theirs on a first `?create=1` ping,
# which runs Healthchecks's `Check.assign_all_channels()`.

import json
import os
import uuid
from django.contrib.auth import get_user_model
from hc.accounts.models import Profile, Project
from hc.api.models import Channel

_hc_email = os.environ["CATENA_ADMIN_EMAIL"]
_hc_pw = os.environ["CATENA_HC_SUPERUSER_PASSWORD"]
_hc_inventory_hostname = os.environ["CATENA_INVENTORY_HOSTNAME"]
_hc_api_key_readonly = os.environ["CATENA_HC_API_KEY_READONLY"]
_hc_api_key_readwrite = os.environ.get("CATENA_HC_API_KEY_READWRITE", "")
_hc_ping_key = os.environ["CATENA_HC_PING_KEY"]
_hc_ntfy_topic = os.environ["CATENA_NTFY_TOPIC"]
_hc_ntfy_server = os.environ["CATENA_NTFY_SERVER"]
_hc_mail_enabled = os.environ["CATENA_MAIL_ENABLED"] == "true"
_NTFY_CHANNEL_CODE = uuid.uuid5(uuid.NAMESPACE_URL, "catena:healthchecks-seed:ntfy")
_EMAIL_CHANNEL_CODE = uuid.uuid5(uuid.NAMESPACE_URL, "catena:healthchecks-seed:admin-email")

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

# The operator's Project owns the seeded checks and channels.
# Project.objects.create() skips the signup flow's tutorial check and email
# channel.
if not Project.objects.filter(owner=operator).exists():
    Project.objects.create(owner=operator, name=_hc_inventory_hostname)

project = Project.objects.filter(owner=operator).first()
if not project:
    raise SystemExit("Healthchecks project bootstrap failed unexpectedly")

project.api_key_readonly = _hc_api_key_readonly
# The RW key is what catena-gatus-sync's orphan-pause pass and catena-schedule's
# check sync write with: GET /api/v3/checks/ needs readonly, a create, update,
# pause or resume needs the full api_key. Empty value (migrate-path) leaves the
# existing project.api_key in place - HC keeps any prior value.
_save_fields = ["api_key_readonly", "ping_key", "name"]
if _hc_api_key_readwrite:
    project.api_key = _hc_api_key_readwrite
    _save_fields.insert(0, "api_key")
project.ping_key = _hc_ping_key
if not project.name:
    project.name = _hc_inventory_hostname
project.save(update_fields=_save_fields)


def _ensure_channel(code, kind, value, name):
    """Keep the one channel this seed owns under `code` in the project:
    created with `value` and attached to every check, updated in place, or
    deleted when `value` is None. Returns (channel or None, rows deleted).

    Selecting on the fixed code leaves every channel a client added alone, and
    an update leaves the channel's check bindings as they are. An email channel
    is marked verified: Healthchecks sends to an unverified one nothing at all,
    and the admin's address is the install's own."""
    if value is None:
        removed, _ = Channel.objects.filter(project=project, code=code).delete()
        return None, removed
    channel, created = Channel.objects.update_or_create(
        project=project,
        code=code,
        defaults={
            "kind": kind,
            "value": value,
            "name": name,
            "email_verified": kind == "email",
        },
    )
    if created:
        channel.assign_all_checks()
    return channel, 0


# The admin's email is the default alert channel, through the outgoing mail
# chosen in Settings > Mail (healthchecks.compose.yml.j2's EMAIL_* settings).
# Notified on down and on up. With mail turned off nothing could send it, so
# the channel is removed, and alerts reach only ntfy and the channels a client
# added in the Healthchecks integrations UI.
email_channel, _ = _ensure_channel(
    _EMAIL_CHANNEL_CODE,
    "email",
    json.dumps({"value": _hc_email, "up": True, "down": True})
    if _hc_mail_enabled else None,
    "Admin email ({})".format(_hc_inventory_hostname),
)

# The ntfy channel is OPTIONAL, and both halves are required to make one.
# Neither NTFY_SERVER nor NTFY_TOPIC defaults to a value: a default of
# https://ntfy.sh would be public and unauthenticated, where the topic is
# the only access control, so a host nobody configured would push its
# alerts to a server nobody here runs -- the wrong thing to do by
# default. Requiring both also avoids a channel with an empty topic when
# only the server is set: a route that resolves and delivers nowhere, yet
# reads in the UI as configured.
#
# Both blank is a supported end state, not a half-finished install: the checks
# still record every ping and reach the admin's email, and the client attaches
# their own channel through the Healthchecks integrations UI. Clearing the
# values on a converge removes the channel an earlier converge seeded, rather
# than leaving a stale one nobody can see is dead.
if _hc_ntfy_topic and _hc_ntfy_server:
    channel, _ = _ensure_channel(
        _NTFY_CHANNEL_CODE,
        "ntfy",
        json.dumps({
            "topic": _hc_ntfy_topic,
            "url": _hc_ntfy_server,
            "priority": 3,
            "priority_up": 3,
        }),
        "ntfy ({})".format(_hc_inventory_hostname),
    )
else:
    channel, removed = _ensure_channel(_NTFY_CHANNEL_CODE, "ntfy", None, "")
    print(
        "healthchecks-seed: no ntfy channel configured (NTFY_SERVER and "
        "NTFY_TOPIC must both be set); nothing is pushed to ntfy. Removed {} "
        "stale ntfy channel(s).".format(removed)
    )

print(
    "OK channel={} email={}".format(
        channel.code if channel is not None else "none",
        email_channel.code if email_channel is not None else "none",
    )
)
