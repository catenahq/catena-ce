#!/usr/bin/env python3
"""Seed Beszel's hub configuration (idempotent): universal token, OIDC login,
alert delivery and rules, outgoing mail.

Run on the host against the hub's loopback publish. Authenticates to
PocketBase as the superuser holding the admin email and password, then
reconciles five things, the first two being:

1. The single `universal_tokens` row (unique index on `user`). With a
   permanent universal token present, a Beszel agent that connects with
   TOKEN=<that token> auto-registers its system on first WebSocket connect
   (henrygd/beszel internal/hub/agent_connect.go
   createNewSystemForUniversalToken) -- no manual "add system" step.

2. The `oidc` OAuth2 provider on the `users` collection, pointed at Keycloak.
   Beszel is already behind oauth2-proxy, so whoever reaches its login form
   has authenticated to Keycloak and proved `admin` group membership -- and
   was then asked for a password a second time by a different system. This
   turns that prompt into a button.

   PASSWORD LOGIN IS LEFT ON, deliberately. DISABLE_PASSWORD_AUTH is never
   set. Beszel is base-plane infrastructure, not a client app: a monitoring
   tool reachable only through the SSO tool cannot be used to diagnose the SSO
   tool. The local superuser stays as break-glass -- and this script itself
   depends on it, since it authenticates against `_superusers`, a different
   collection from the `users` one OIDC governs.

Then the alert delivery (the Healthchecks shim's webhook, and no Beszel mail
to the admin), the default alert rules and the hub's outgoing mail, each
explained at its function below.

A hub started on an empty volume has no user and a placeholder superuser, and
serves its first-user endpoint until someone calls it. The seed calls it first,
as the admin (claim_first_run), so the hub's spec never carries the password.
The seed keeps the admin email it last signed in with in its state file, and
after an admin email change signs in with that one and moves both records to
the new address (follow_admin_email).

Config is one JSON object on stdin, so no credential is in the process's argv
or environment, which `ps` shows every local user (KeyErrors loudly on any
wiring break):

    hub_url             e.g. http://127.0.0.1:18190
    state_file          the seed's state file (beszel_seed_state_file)
    admin_email         superuser identity (== inventory admin_email)
    admin_password      superuser password (admin_password)
    universal_token     the token to seed (beszel_universal_token)
    oidc_client_id      Keycloak clientId (realm-beszel.yaml.j2)
    oidc_client_secret  beszel_oidc_client_secret
    oidc_auth_url       .../protocol/openid-connect/auth
    oidc_token_url      .../protocol/openid-connect/token
    oidc_userinfo_url   .../protocol/openid-connect/userinfo
    oidc_display_name   label on the hub's sign-in button
    alert_webhook       the shim's Shoutrrr URL
    smtp_*              outgoing mail (optional; blank host = none)

Exit 0 on success, printing one line carrying every outcome so the caller's
changed_when can read it. Any failure exits non-zero with a stderr message so
the calling Ansible task's retry loop can wait out hub start-up.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 10


def _req(method: str, url: str, *, token: str | None = None, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        # PocketBase accepts the raw auth token in the Authorization header.
        headers["Authorization"] = token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        raw = resp.read().decode()
    return json.loads(raw) if raw else {}


def die(msg: str) -> None:
    print(msg, file=sys.stderr)
    sys.exit(1)


def read_state(path: str) -> dict:
    """What the last run recorded: the admin email it signed in with and the
    fingerprint of the mail password it wrote. {} when there is no record."""
    try:
        with open(path, encoding="utf-8") as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def write_state(path: str, state: dict) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(state, fh)


def sign_in(base: str, identities: list[str], password: str) -> tuple[dict, str]:
    """Authenticate as the superuser under the first identity the hub
    accepts. Returns (auth response, identity)."""
    for identity in identities:
        try:
            return _req(
                "POST",
                f"{base}/api/collections/_superusers/auth-with-password",
                body={"identity": identity, "password": password},
            ), identity
        except urllib.error.HTTPError as exc:
            if exc.code != 400:
                raise
        except urllib.error.URLError as exc:
            die(f"superuser auth failed (hub not ready yet?): {exc}")
    die(f"no superuser for {identities!r} (hub bootstrap incomplete?)")


def claim_first_run(base: str, email: str, password: str) -> None:
    """Make the admin the hub's first user and superuser while it has no user
    (beszel v0.20.0 internal/hub/api.go first-run and create-user,
    internal/users/users.go CreateFirstUser, which also removes the
    placeholder superuser). A hub that has a user is left alone; one claimed by
    somebody else answers 403, which fails the seed."""
    try:
        first = _req("GET", f"{base}/api/beszel/first-run").get("firstRun")
    except urllib.error.URLError as exc:
        die(f"first-run check failed (hub not ready yet?): {exc}")
    if first is True:
        _req("POST", f"{base}/api/beszel/create-user",
             body={"email": email, "password": password})


def follow_admin_email(base: str, auth: dict, old: str, new: str) -> None:
    """Move the superuser and the admin's users record from `old` to `new`.
    The users record keeps its id, so the universal token, the alert settings
    and the rules that point at it stay attached."""
    su_token = auth["token"]
    _req(
        "PATCH",
        f"{base}/api/collections/_superusers/records/{auth['record']['id']}",
        token=su_token,
        body={"email": new},
    )
    flt = urllib.parse.quote(f"email='{old}'")
    users = _req(
        "GET",
        f"{base}/api/collections/users/records?perPage=1&filter=({flt})",
        token=su_token,
    ).get("items") or []
    if users:
        _req(
            "PATCH",
            f"{base}/api/collections/users/records/{users[0]['id']}",
            token=su_token,
            body={"email": new},
        )


def main() -> int:
    cfg = json.load(sys.stdin)
    try:
        base = cfg["hub_url"].rstrip("/")
        state_file = cfg["state_file"]
        email = cfg["admin_email"]
        password = cfg["admin_password"]
        token = cfg["universal_token"]
        oidc = {
            "client_id": cfg["oidc_client_id"],
            "client_secret": cfg["oidc_client_secret"],
            "auth_url": cfg["oidc_auth_url"],
            "token_url": cfg["oidc_token_url"],
            "userinfo_url": cfg["oidc_userinfo_url"],
            "display_name": cfg["oidc_display_name"],
        }
        webhook = cfg["alert_webhook"]
        # Outgoing mail, resolved by the converge from the ONE stored choice
        # (catena_smtp_resolve). Blank host means the host has no mail
        # configured, which is a supported state and disables Beszel's mail
        # rather than failing.
        smtp = {
            "host": cfg.get("smtp_host", ""),
            "port": cfg.get("smtp_port", "587"),
            "user": cfg.get("smtp_user", ""),
            "password": cfg.get("smtp_password", ""),
            "sender": cfg.get("smtp_sender", ""),
            "app_name": cfg.get("smtp_app_name", "Catena"),
        }
    except KeyError as exc:
        die(f"missing required config key: {exc}")

    # 1. Authenticate as the superuser, claiming a hub that has no user yet,
    # under the admin email the last run recorded when the hub does not know
    # the current one yet.
    claim_first_run(base, email, password)
    state = read_state(state_file)
    recorded = str(state.get("admin_email") or "")
    auth, signed_in_as = sign_in(
        base, [email] + ([recorded] if recorded and recorded != email else []),
        password)
    if signed_in_as != email:
        follow_admin_email(base, auth, signed_in_as, email)
        auth, _ = sign_in(base, [email], password)
    su_token = auth.get("token")
    if not su_token:
        die("superuser auth returned no token")
    state["admin_email"] = email
    write_state(state_file, state)

    # 2. Resolve the users record id for the relation.
    flt = urllib.parse.quote(f"email='{email}'")
    users = _req(
        "GET",
        f"{base}/api/collections/users/records?perPage=1&filter=({flt})",
        token=su_token,
    )
    items = users.get("items") or []
    if not items:
        die(f"no users record for {email!r} (hub bootstrap incomplete?)")
    user_id = items[0]["id"]

    # 3. Find-or-update the universal_tokens row (unique on user).
    user_flt = urllib.parse.quote(f"user='{user_id}'")
    existing = _req(
        "GET",
        f"{base}/api/collections/universal_tokens/records?perPage=1&filter=({user_flt})",
        token=su_token,
    )
    rows = existing.get("items") or []
    if not rows:
        _req(
            "POST",
            f"{base}/api/collections/universal_tokens/records",
            token=su_token,
            body={"user": user_id, "token": token},
        )
        token_state = "minted universal token"
    elif rows[0].get("token") == token:
        token_state = "ok-exists"
    else:
        _req(
            "PATCH",
            f"{base}/api/collections/universal_tokens/records/{rows[0]['id']}",
            token=su_token,
            body={"token": token},
        )
        token_state = "updated universal token"

    # 4. The OIDC provider (configure_oidc).
    #
    # Runs regardless of what the token step did: the two answer different
    # questions (can an agent auto-register / can a person sign in with
    # Catena), and returning early on an already-current token would leave the
    # login unconfigured forever on any host whose token was already right.
    oidc_state = configure_oidc(base, su_token, oidc)

    # 5. The alert DELIVERY channel, then the rules that deliver into it.
    #
    # Seeding both is what keeps the alert path off the list of manual UI
    # steps. Left to the UI it is silently absent on every fresh host -- shim
    # deployed, thresholds unset, nothing saying so -- and a monitor that is
    # watching and cannot tell anybody is the failure mode monitoring exists to
    # avoid.
    delivery_state = configure_alert_delivery(base, su_token, user_id, webhook, email)
    rules_state = configure_alert_rules(base, su_token, user_id)

    # 6. Outgoing mail. Beszel is PocketBase, so this is the app's own mail
    # settings: alert mail to an address a client adds in the hub, and the
    # hub's own account mail.
    smtp_state, state["smtp_password_sha256"] = configure_smtp(
        base, su_token, smtp, str(state.get("smtp_password_sha256") or ""))
    write_state(state_file, state)

    # One line carrying all four, because the caller's changed_when reads
    # stdout. "ok-exists" from every one is the steady-state converge; anything
    # else is a real write and must report changed.
    print(
        f"universal-token: {token_state}; oidc: {oidc_state}; "
        f"alert-delivery: {delivery_state}; alert-rules: {rules_state}; "
        f"smtp: {smtp_state}"
    )
    return 0


# ─── alert delivery + rules ────────────────────────────────────────────────
# Beszel notifies through Shoutrrr URLs held on the user's own settings record,
# and fires them from per-system threshold rules. Both were left to the hub UI,
# so a converged host had a deployed alert shim, no webhook pointed at it and no
# rule that would ever fire -- a monitor watching a server with no way to tell
# anybody, which is the one failure monitoring exists to avoid.
#
# Verified against beszel v0.18.7 rather than assumed:
#   internal/alerts/alerts.go  reads collection `user_settings`, field
#                              `settings`, into {emails: [], webhooks: []}
#   the collections snapshot   defines `alerts` with user + system relations,
#                              a `name` SELECT, and numeric `value` / `min`
#   internal/alerts/alerts_system.go
#                              `min` is MINUTES -- time.Duration(min)*time.Minute,
#                              reported as "averaged X for the previous N minutes"
#                              -- and `value` is the threshold.

# The `name` field is a SELECT: a value outside this set is refused by
# PocketBase, so these are copied from the collection definition rather than
# invented.
_ALERT_STATUS = "Status"

# What a single-VPS host should be told about, with the thresholds a human
# would pick. Deliberately few: an alert set nobody reads is the same as no
# alerts, and each of these is a condition somebody would want woken for.
#
# `min` is the sustain window in minutes, which is what keeps a momentary spike
# during a backup or a container update from paging anyone.
_DEFAULT_RULES = (
    # Status has no threshold -- it fires when the agent stops reporting. The
    # window is short because "the server is gone" does not get truer by
    # waiting.
    {"name": _ALERT_STATUS, "value": 0, "min": 5},
    {"name": "CPU", "value": 90, "min": 10},
    {"name": "Memory", "value": 90, "min": 10},
    {"name": "Disk", "value": 85, "min": 10},
)


def configure_alert_delivery(base: str, su_token: str, user_id: str,
                             webhook: str, admin_email: str) -> str:
    """Point the user's notifications at the Healthchecks shim, and keep the
    admin's address off Beszel's own alert mail.

    A Beszel alert reaches Healthchecks through the shim, and Healthchecks
    mails the admin (healthchecks-seed.py's email channel). Mailed by Beszel
    as well, the admin would get two emails for one alert. Every other
    address a client adds in the hub keeps Beszel's mail.

    Returns one of: ok-exists / configured / updated.
    """
    flt = urllib.parse.quote(f"user='{user_id}'")
    found = _req(
        "GET",
        f"{base}/api/collections/user_settings/records?perPage=1&filter=({flt})",
        token=su_token,
    )
    rows = found.get("items") or []
    current = {}
    if rows:
        current = dict(rows[0].get("settings") or {})
    hooks = list(current.get("webhooks") or [])
    emails = list(current.get("emails") or [])
    kept = [e for e in emails if e.strip().lower() != admin_email.strip().lower()]
    if webhook in hooks and kept == emails:
        return "ok-exists"

    # APPEND, never replace. A client who added their own ntfy or Slack URL in
    # the hub UI keeps it -- the same rule the OIDC provider merge follows, and
    # for the same reason: this converge owns one entry, not the list.
    merged = dict(current)
    merged["webhooks"] = hooks if webhook in hooks else hooks + [webhook]
    merged["emails"] = kept

    if rows:
        _req(
            "PATCH",
            f"{base}/api/collections/user_settings/records/{rows[0]['id']}",
            token=su_token,
            body={"settings": merged},
        )
        return "updated"
    created = _req(
        "POST",
        f"{base}/api/collections/user_settings/records",
        token=su_token,
        body={"user": user_id, "settings": merged},
    )
    # Beszel's create hook replaces `emails` with the account's own address
    # (internal/users/users.go InitializeUserSettings, beszel v0.20.0), so the
    # new record is written again, which no hook rewrites.
    _req(
        "PATCH",
        f"{base}/api/collections/user_settings/records/{created['id']}",
        token=su_token,
        body={"settings": merged},
    )
    return "configured"


def configure_alert_rules(base: str, su_token: str, user_id: str) -> str:
    """Create the default threshold rules for every registered system.

    Returns "no-systems" when the agent has not registered yet, which is the
    NORMAL state on a first converge: this script runs before the agent is
    deployed, because the agent needs the universal token this same script
    seeds. The task that calls it again after the agent deploy is what closes
    that gap, and it is why "no-systems" is reported rather than treated as an
    error.
    """
    systems = _req(
        "GET",
        f"{base}/api/collections/systems/records?perPage=200",
        token=su_token,
    )
    rows = systems.get("items") or []
    if not rows:
        return "no-systems"

    created = 0
    for system in rows:
        sid = system["id"]
        sys_flt = urllib.parse.quote(f"user='{user_id}' && system='{sid}'")
        existing = _req(
            "GET",
            f"{base}/api/collections/alerts/records?perPage=200&filter=({sys_flt})",
            token=su_token,
        )
        have = {r.get("name") for r in (existing.get("items") or [])}
        for rule in _DEFAULT_RULES:
            if rule["name"] in have:
                continue
            # Created, never updated: a client who raised the CPU threshold
            # because their server runs hot meant to do that, and a converge
            # that reset it every night would be worse than no default at all.
            _req(
                "POST",
                f"{base}/api/collections/alerts/records",
                token=su_token,
                body={
                    "user": user_id,
                    "system": sid,
                    "name": rule["name"],
                    "value": rule["value"],
                    "min": rule["min"],
                },
            )
            created += 1
    return "ok-exists" if created == 0 else f"created {created}"


# ─── outgoing mail ─────────────────────────────────────────────────────────
# The host's own mail settings, so the hub can send its account mail and the
# alert mail to any address a client adds in the hub. The admin's alerts go
# through Healthchecks instead (configure_alert_delivery).
#
# Verified against pocketbase v0.36.8 (which beszel 0.18.7 vendors) rather than
# assumed -- core.SMTPConfig tags enabled / port / host / username / password /
# authMethod / tls / localName, and core.MetaConfig tags senderName /
# senderAddress. The endpoint is PATCH /api/settings, which merges.
#
# `tls: false` is CORRECT and not a downgrade. PocketBase's own comment: "When
# set to false StartTLS command is send, leaving the server to decide whether
# to upgrade the connection or not." True means implicit TLS from the first
# byte, which is port 465; every path this product resolves is STARTTLS on the
# port given, and both known providers expect that on 587.

# Compared to decide whether a write is needed. `password` is not among them:
# PocketBase blanks it in every settings read (core/settings_model.go
# Settings.MarshalJSON, pocketbase v0.40.4, which beszel 0.20.0 vendors). The
# seed compares the fingerprint of the password it last wrote instead, so a
# new password alone is written, and an unchanged one reports no change.
_SMTP_COMPARED = ("enabled", "host", "port", "username", "tls")


def _fingerprint(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def _desired_smtp(cfg: dict) -> dict:
    try:
        port = int(str(cfg.get("port") or "587").strip())
    except ValueError:
        port = 587
    return {
        "enabled": bool(str(cfg.get("host") or "").strip()),
        "host": str(cfg.get("host") or "").strip(),
        "port": port,
        "username": str(cfg.get("user") or "").strip(),
        "password": str(cfg.get("password") or ""),
        # PLAIN is PocketBase's default and what both known relays accept.
        "authMethod": "PLAIN",
        # STARTTLS, not implicit TLS. See the note above.
        "tls": False,
    }


def configure_smtp(base: str, su_token: str, cfg: dict,
                   written: str) -> tuple[str, str]:
    """Point Beszel's mail at the host's configured relay. `written` is the
    fingerprint of the password the last write sent.

    Returns (one of ok-exists / no-mail / disabled / configured / updated, the
    fingerprint of the password the hub now holds).
    """
    desired = _desired_smtp(cfg)
    current_all = _req("GET", f"{base}/api/settings", token=su_token)
    current = dict(current_all.get("smtp") or {})
    if not desired["enabled"]:
        # A host with no mail configured is a supported state -- SMTP_PROVIDER
        # can be `none`. Mail turned off turns the hub's off too, so the
        # credential it held stops being used. Reported rather than skipped
        # silently, so the caller's changed_when can tell it apart from a write.
        if current.get("enabled"):
            _req("PATCH", f"{base}/api/settings", token=su_token,
                 body={"smtp": {"enabled": False}})
            return "disabled", ""
        return "no-mail", ""

    meta = dict(current_all.get("meta") or {})
    sender = str(cfg.get("sender") or "").strip()
    app_name = str(cfg.get("app_name") or "Catena").strip()
    fingerprint = _fingerprint(desired["password"])

    smtp_same = all(
        str(current.get(k, "")) == str(desired[k]) for k in _SMTP_COMPARED
    )
    meta_same = (
        str(meta.get("senderAddress", "")) == sender
        and str(meta.get("senderName", "")) == app_name
    )
    if smtp_same and meta_same and written == fingerprint:
        return "ok-exists", fingerprint

    had_host = bool(str(current.get("host") or "").strip())
    _req(
        "PATCH",
        f"{base}/api/settings",
        token=su_token,
        # meta goes in the same PATCH: an SMTP server with no sender address
        # produces mail most relays refuse, and the two are one setting from
        # the operator's point of view.
        body={
            "smtp": desired,
            "meta": {"senderName": app_name, "senderAddress": sender},
        },
    )

    # READ BACK, for the reason the OIDC provider is read back: PocketBase
    # drops keys it does not recognise rather than rejecting them, so a field
    # rename on a future bump would leave mail configured, reporting success,
    # and silently never sending.
    after = dict((_req("GET", f"{base}/api/settings", token=su_token)
                  ).get("smtp") or {})
    drifted = [
        k for k in _SMTP_COMPARED if str(after.get(k, "")) != str(desired[k])
    ]
    if drifted:
        die(
            f"wrote the mail settings but {drifted} did not land -- PocketBase "
            "ignored unrecognised keys, so these names have drifted from "
            "core.SMTPConfig"
        )
    return ("updated" if had_host else "configured"), fingerprint


# ─── OIDC provider on the users collection ─────────────────────────────────
# What it is for, and why password login stays on: the module docstring, item
# 2. tests/unit/test_beszel_oidc.py asserts DISABLE_PASSWORD_AUTH never
# appears in the role.
#
# `oidc` is PocketBase's generic OpenID Connect provider key (tools/auth/
# oidc.go registers oidc, oidc2, oidc3 against one factory). Field names are
# the json tags of core.OAuth2ProviderConfig at the version Beszel vendors --
# authURL / tokenURL / userInfoURL, NOT the pre-v0.23 authUrl / userApiUrl
# spelling, which PocketBase would accept as unknown keys and silently ignore.
# That is why the write is read back below rather than assumed.
_PROVIDER = "oidc"

# Compared to decide whether a write is needed. clientSecret is excluded: it
# may be masked on read, and a field that cannot be read back cannot be
# compared without reporting `changed` on every converge.
_COMPARED = ("name", "clientId", "authURL", "tokenURL", "userInfoURL", "displayName")


def _desired_provider(cfg: dict) -> dict:
    return {
        "name": _PROVIDER,
        "clientId": cfg["client_id"],
        "clientSecret": cfg["client_secret"],
        "authURL": cfg["auth_url"],
        "tokenURL": cfg["token_url"],
        "userInfoURL": cfg["userinfo_url"],
        "displayName": cfg["display_name"],
    }


def _provider_matches(current: dict, desired: dict) -> bool:
    """True when a write would change nothing observable.

    The secret is compared only when the hub actually returns one. PocketBase
    tags clientSecret `omitempty`, so an absent value means "not readable
    here", which is not the same as "differs" -- treating it as a difference
    would make every converge report changed and fail the idempotency rerun.
    """
    if any(str(current.get(k, "")) != str(desired[k]) for k in _COMPARED):
        return False
    seen_secret = str(current.get("clientSecret", ""))
    return not seen_secret or seen_secret == desired["clientSecret"]


def configure_oidc(base: str, su_token: str, cfg: dict) -> str:
    """Find-or-update the `oidc` provider on the users collection.

    Returns one of: ok-exists / configured / updated.
    """
    users = _req("GET", f"{base}/api/collections/users", token=su_token)
    oauth2 = dict(users.get("oauth2") or {})
    providers = list(oauth2.get("providers") or [])
    desired = _desired_provider(cfg)

    existing = next((p for p in providers if p.get("name") == _PROVIDER), None)
    if existing and oauth2.get("enabled") and _provider_matches(existing, desired):
        return "ok-exists"

    # Preserve any other provider the client configured by hand; replace only
    # ours. A blanket overwrite would silently delete a working GitHub login.
    merged = [p for p in providers if p.get("name") != _PROVIDER] + [desired]
    _req(
        "PATCH",
        f"{base}/api/collections/users",
        token=su_token,
        body={"oauth2": {"enabled": True, "providers": merged}},
    )

    # READ BACK. A key PocketBase does not recognise is dropped rather than
    # rejected, so a field-name drift on a future bump would leave a provider
    # that exists, reports success, and cannot complete a login. Verifying is
    # the only way that surfaces as a converge failure instead of as a broken
    # button.
    after = _req("GET", f"{base}/api/collections/users", token=su_token)
    after_oauth2 = after.get("oauth2") or {}
    landed = next(
        (p for p in (after_oauth2.get("providers") or [])
         if p.get("name") == _PROVIDER),
        None,
    )
    if not landed:
        die(f"wrote the {_PROVIDER} provider but it is absent on read-back")
    if not after_oauth2.get("enabled"):
        die("wrote the oauth2 config but the users collection reports it disabled")
    missing = [k for k in _COMPARED if str(landed.get(k, "")) != str(desired[k])]
    if missing:
        die(
            f"{_PROVIDER} provider landed with {missing} not matching what was "
            "sent -- PocketBase dropped unrecognised keys, so these field names "
            "have drifted from core.OAuth2ProviderConfig"
        )
    return "updated" if existing else "configured"


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except urllib.error.HTTPError as exc:
        die(f"HTTP {exc.code} from hub API: {exc.read().decode(errors='replace')[:300]}")
    except urllib.error.URLError as exc:
        die(f"hub API unreachable: {exc}")
