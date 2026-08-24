#!/usr/bin/env python3
"""Seed Beszel's hub configuration: universal token + OIDC login (idempotent).

Run on the host against the hub's loopback publish. Authenticates to
PocketBase as the superuser bootstrapped from USER_EMAIL/USER_PASSWORD, then
reconciles two things:

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

Config via env (mirrors the healthchecks-seed.py contract; KeyErrors loudly on
any wiring break):

    BESZEL_HUB_URL             e.g. http://127.0.0.1:18190
    BESZEL_ADMIN_EMAIL         superuser identity (== inventory admin_email)
    BESZEL_ADMIN_PASSWORD      superuser password (admin_password)
    BESZEL_UNIVERSAL_TOKEN     the token to seed (beszel_universal_token)
    BESZEL_OIDC_CLIENT_ID      Keycloak clientId (realm-beszel.yaml.j2)
    BESZEL_OIDC_CLIENT_SECRET  beszel_oidc_client_secret
    BESZEL_OIDC_AUTH_URL       .../protocol/openid-connect/auth
    BESZEL_OIDC_TOKEN_URL      .../protocol/openid-connect/token
    BESZEL_OIDC_USERINFO_URL   .../protocol/openid-connect/userinfo
    BESZEL_OIDC_DISPLAY_NAME   label on the hub's sign-in button

Exit 0 on success, printing one line carrying both outcomes so the caller's
changed_when can read it. Any failure exits non-zero with a stderr message so
the calling Ansible task's retry loop can wait out hub start-up.
"""

from __future__ import annotations

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


def main() -> int:
    try:
        base = os.environ["BESZEL_HUB_URL"].rstrip("/")
        email = os.environ["BESZEL_ADMIN_EMAIL"]
        password = os.environ["BESZEL_ADMIN_PASSWORD"]
        token = os.environ["BESZEL_UNIVERSAL_TOKEN"]
        oidc = {
            "client_id": os.environ["BESZEL_OIDC_CLIENT_ID"],
            "client_secret": os.environ["BESZEL_OIDC_CLIENT_SECRET"],
            "auth_url": os.environ["BESZEL_OIDC_AUTH_URL"],
            "token_url": os.environ["BESZEL_OIDC_TOKEN_URL"],
            "userinfo_url": os.environ["BESZEL_OIDC_USERINFO_URL"],
            "display_name": os.environ["BESZEL_OIDC_DISPLAY_NAME"],
        }
    except KeyError as exc:
        die(f"missing required env var: {exc}")

    # 1. Authenticate as the bootstrapped superuser.
    try:
        auth = _req(
            "POST",
            f"{base}/api/collections/_superusers/auth-with-password",
            body={"identity": email, "password": password},
        )
    except urllib.error.URLError as exc:
        die(f"superuser auth failed (hub not ready yet?): {exc}")
    su_token = auth.get("token")
    if not su_token:
        die("superuser auth returned no token")

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

    # 4. Find-or-update the OIDC provider on the users collection.
    #
    # Runs regardless of what the token step did: the two answer different
    # questions (can an agent auto-register / can a person sign in with
    # Catena), and returning early on an already-current token would leave the
    # login unconfigured forever on any host whose token was already right.
    oidc_state = configure_oidc(base, su_token, oidc)

    # One line carrying both, because the caller's changed_when reads stdout.
    # "ok-exists" from BOTH is the steady-state converge; anything else is a
    # real write and must report changed.
    print(f"universal-token: {token_state}; oidc: {oidc_state}")
    return 0


# ─── OIDC provider on the users collection ─────────────────────────────────
# Beszel sits behind oauth2-proxy already, so a client reaching its login form
# has authenticated to Keycloak and proved `admin` group membership -- and was
# then asked for a password a second time, by PocketBase, for access already
# granted. This configures Keycloak as an OIDC provider so that prompt becomes
# a button.
#
# PASSWORD LOGIN IS LEFT ON. Nothing here sets DISABLE_PASSWORD_AUTH, and
# tests/unit/test_beszel_oidc.py asserts the string never appears in the role.
# Beszel is base-plane infrastructure: a monitoring tool reachable only through
# the SSO tool cannot be used to diagnose the SSO tool.
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
