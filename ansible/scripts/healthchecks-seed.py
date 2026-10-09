"""Seed and reconcile self-hosted Healthchecks: the operator superuser, the
project and its API keys, and the admin's email channel. It creates no check.
Re-running reconciles drift and leaves client-added checks and channels
alone."""
# Managed by Ansible (reconcile/roles/infrastructure). Do not edit by hand.
#
# Runs inside the Healthchecks container via `docker exec -i ... python
# manage.py shell <`. Per-host values arrive as `docker exec -e KEY=VALUE`
# flags rendered in reconcile/roles/infrastructure/tasks/healthchecks.yml; a
# missing one raises KeyError, so a wiring break fails loud.
#
# Seeds:
#   0. The operator superuser and Project. The upstream image runs migrations
#      only, so a fresh container has an empty auth_user table, and the
#      oauth2-proxy hop (X-Forwarded-Email) needs a User row to map onto.
#   1. Project.api_key_readonly, ping_key and name, pinned to the on-box store.
#   2. Catena's channel, the admin's email while outgoing mail is on, through
#      _ensure_channel. It carries a fixed `code`, so a converge updates or
#      removes that one row and never a channel it does not own. It is
#      attached to every check when it is created, and never again, so a
#      client who detaches it from a check keeps that choice.
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
_hc_mail_enabled = os.environ["CATENA_MAIL_ENABLED"] == "true"
_EMAIL_CHANNEL_CODE = uuid.uuid5(uuid.NAMESPACE_URL, "catena:healthchecks-seed:admin-email")

User = get_user_model()

# The operator is the owner of the project this seed pins by its ping key, so
# an admin email change renames that one superuser. Looked up by the email
# instead, a new address would get a second superuser, whose project could
# never take the ping key (Project.ping_key is unique). With no pinned project
# yet, it is the user named after the admin email, created when missing.
#
# username = email, so the oauth2-proxy identity hop
# (REMOTE_USER_HEADER=HTTP_X_FORWARDED_EMAIL, matched on the email) lands the
# admin in this account. The password is set on creation only: one changed
# in /admin/ is kept.
project = Project.objects.filter(ping_key=_hc_ping_key).first()
operator = project.owner if project else User.objects.filter(username=_hc_email).first()
if operator is None:
    operator = User.objects.create_superuser(
        username=_hc_email, email=_hc_email, password=_hc_pw,
    )
else:
    _wanted = {"username": _hc_email, "email": _hc_email,
               "is_superuser": True, "is_staff": True}
    _changed = [f for f, v in _wanted.items() if getattr(operator, f) != v]
    for _field in _changed:
        setattr(operator, _field, _wanted[_field])
    if _changed:
        operator.save(update_fields=_changed)

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

# The operator's Project owns the seeded channel. Project.objects.create()
# skips the signup flow's tutorial check and email channel.
if project is None:
    project = Project.objects.filter(owner=operator).first()
if project is None:
    project = Project.objects.create(owner=operator, name=_hc_inventory_hostname)

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
    deleted when `value` is None. Returns the channel, or None.

    Selecting on the fixed code leaves every channel a client added alone, and
    an update leaves the channel's check bindings as they are. An email channel
    is marked verified: Healthchecks sends to an unverified one nothing at all,
    and the admin's address is the install's own."""
    if value is None:
        Channel.objects.filter(project=project, code=code).delete()
        return None
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
    return channel


# The admin's email is the alert channel, through the outgoing mail chosen in
# Settings > Mail (healthchecks.compose.yml.j2's EMAIL_* settings). Notified
# on down and on up. With mail turned off nothing could send it, so the
# channel is removed, and alerts reach only the channels a client added in the
# Healthchecks integrations UI.
email_channel = _ensure_channel(
    _EMAIL_CHANNEL_CODE,
    "email",
    json.dumps({"value": _hc_email, "up": True, "down": True})
    if _hc_mail_enabled else None,
    "Admin email ({})".format(_hc_inventory_hostname),
)

print("OK email={}".format(email_channel.code if email_channel is not None else "none"))
