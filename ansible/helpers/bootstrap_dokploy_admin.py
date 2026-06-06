#!/usr/bin/env python3
"""Leaf utility: auto-provision Dokploy admin + mint API key via better-auth.

Dokploy v0.29.x uses better-auth as its auth framework (see
packages/server/src/lib/auth.ts upstream). The admin user and the
x-api-key used by the rest of the stack are both creatable over HTTP
WITHOUT a browser, via better-auth's public endpoints:

  POST /api/auth/sign-up/email      {email, password, name, lastName}
    -> On fresh installs, the FIRST signup becomes admin + owner of a
       default organization (see Dokploy's `after` hook in its
       betterAuth config). Subsequent signups get
       400 {message: "Admin is already created"}.

  POST /api/auth/sign-in/email      {email, password}
    -> 200 with Set-Cookie: better-auth.session_token=...

  POST /api/auth/api-key/create     (with session cookie)  {name}
    -> 200 {key: "..."} -- standard @better-auth/api-key plugin.

This helper chains those three calls using the shared vault_admin_password
(already in the vault, shared with Keycloak admin) + ADMIN_EMAIL from
the inventory .env. On success it merges the minted API key into the
vault under vault_dokploy_api_key and exits 0. Invoked by the installer
(and by roles/dokploy on a converge) when the vault does not yet hold a
valid dokploy API key.

Contract:
  Exit code 0  : API key minted + merged into vault.
  Exit code 2  : Dokploy unreachable at tailnet:port -- caller should fall
                 back to the interactive prompt. Not a hard failure;
                 Dokploy may not have finished booting yet.
  Exit code 3  : Dokploy reachable but admin already exists with
                 DIFFERENT credentials than vault_admin_password (signin
                 returned 401). Caller should fall back to the prompt
                 -- the operator either created the admin via the UI
                 with a different password, or vault_admin_password was
                 rotated after install. We don't try to reset it.
  Exit code 1  : unexpected error (vault I/O, unexpected HTTP code,
                 malformed responses, etc.). Caller should surface the
                 message -- not auto-fallback to the prompt.

Usage:
    uv run helpers/bootstrap_dokploy_admin.py \\
        --vault inventory/dev/group_vars/all/vault.sops.yml \\
        --env-file inventory/dev/.env \\
        --tailnet-ip 100.77.16.46 \\
        --port 3000

Requires SOPS_AGE_KEY in env (raw operator age private key content,
pasted into the calling shell once per session; no key file on disk).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
# Make helpers/ importable in both script and module modes (see
# prompt_dokploy_key.py for the rationale).
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
from helpers import sops_vault  # noqa: E402

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_UNREACHABLE = 2
EXIT_ADMIN_MISMATCH = 3

# better-auth's default cookie name for the Dokploy deployment. No custom
# sessionCookieName in upstream's betterAuth config -> the library default
# ("better-auth.session_token") wins.
SESSION_COOKIE_NAME = "better-auth.session_token"

# Name we tag the API key with in Dokploy's UI. Makes it obvious which
# key came from the installer (vs. any the operator minted later).
API_KEY_LABEL = "catena-installer"

# Better-auth requires first-name AND last-name at signup. The operator
# renames them in the Dokploy UI if they care -- not security-sensitive.
ADMIN_DISPLAY_FIRST = "Admin"
ADMIN_DISPLAY_LAST = "Operator"

# HTTP timeouts: Dokploy's bcrypt on signup can take ~800ms; the rest are
# instant. 10s per call is generous but not infinite.
HTTP_TIMEOUT = 10.0

# Reachability probe window. After site.yml's first pass, Dokploy's
# compose has been deployed but the server container may still be
# starting (Drizzle migrations + Next.js boot ≈ 20-40s). Block here
# until the HTTP port responds with anything (even a 4xx is proof of
# life), or give up and let the caller fall back to the manual prompt.
REACHABILITY_TIMEOUT_S = 120
REACHABILITY_POLL_S = 3


def _warn(msg: str) -> None:
    print(f"\033[1;33m!\033[0m {msg}", file=sys.stderr)


def _ok(msg: str) -> None:
    print(f"\033[1;32m+\033[0m {msg}", file=sys.stderr)


def _info(msg: str) -> None:
    print(f"\033[1;34m-\033[0m {msg}", file=sys.stderr)


# --- vault read/write ------------------------------------------------------
# Vault I/O delegates to helpers/sops_vault.py (single source of truth
# for the sops CLI invocation). See docstring there.


# --- .env read -------------------------------------------------------------
def read_env_value(env_file: Path, key: str) -> str:
    """Minimal dotenv reader -- returns value for KEY=VALUE line, or ""
    if absent. Strips surrounding quotes. We don't need full shell
    semantics here; catena's .env files are flat KEY=VALUE."""
    if not env_file.is_file():
        return ""
    for raw in env_file.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() != key:
            continue
        val = v.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
            val = val[1:-1]
        return val
    return ""


# --- better-auth HTTP primitives -------------------------------------------
class DokployUnreachable(RuntimeError):
    """Thrown when the Dokploy base URL can't be reached at all."""


class AdminCredentialsMismatch(RuntimeError):
    """Thrown when signin fails with 401 against a created admin."""


def _get_json(url: str, cookie: str | None = None,
              origin: str | None = None) -> tuple[int, dict, dict]:
    """GET JSON -- same contract as _post_json but for the GET-only
    endpoints (better-auth's organization/list, get-session)."""
    headers = {"accept": "application/json"}
    if cookie:
        headers["cookie"] = cookie
    if origin:
        headers["origin"] = origin
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            return r.status, _parse_json_or_list(r.read()), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, _parse_json_or_list(e.read()), dict(e.headers or {})
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise DokployUnreachable(str(e)) from e


def _parse_json_or_list(raw: bytes):
    """Like _parse_json but tolerates list responses (better-auth's
    organization/list returns an array, not an object)."""
    try:
        parsed = json.loads(raw.decode("utf-8") or "{}")
        return parsed  # list OR dict -- caller handles shape.
    except (ValueError, UnicodeDecodeError):
        return {}


def _post_json(url: str, body: dict, cookie: str | None = None,
               origin: str | None = None) -> tuple[int, dict, dict]:
    """POST JSON and return (status, parsed-body-or-{}, response-headers).
    Raises DokployUnreachable on connection failures.

    `origin` populates the Origin header -- better-auth's apiKey plugin
    enforces a trustedOrigins check that rejects requests without it
    ('403 Missing or null Origin'). Browsers set it automatically;
    urllib does not. Callers pass `base_url` so the header matches
    where we're actually talking to (which is the default trusted
    origin in Dokploy's better-auth config)."""
    data = json.dumps(body).encode("utf-8")
    headers = {"content-type": "application/json",
               "accept": "application/json"}
    if cookie:
        headers["cookie"] = cookie
    if origin:
        headers["origin"] = origin
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            return r.status, _parse_json(r.read()), dict(r.headers)
    except urllib.error.HTTPError as e:
        # better-auth returns JSON bodies on 4xx/5xx; capture them.
        return e.code, _parse_json(e.read()), dict(e.headers or {})
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise DokployUnreachable(str(e)) from e


def _parse_json(raw: bytes) -> dict:
    try:
        parsed = json.loads(raw.decode("utf-8") or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, UnicodeDecodeError):
        return {}


def better_auth_signup(base_url: str, email: str, password: str,
                       first: str = ADMIN_DISPLAY_FIRST,
                       last: str = ADMIN_DISPLAY_LAST) -> bool:
    """POST /api/auth/sign-up/email.

    Returns:
      True  -- 200, admin just created.
      False -- 400 with 'Admin is already created' message; someone (us
              on a prior partial run, or the operator via UI) beat us
              to it. Caller should proceed to signin.
    Raises:
      DokployUnreachable -- TCP/connection error.
      RuntimeError        -- any other non-success response (500, etc).
    """
    status, body, _ = _post_json(
        f"{base_url}/api/auth/sign-up/email",
        {"email": email, "password": password, "name": first, "lastName": last},
        origin=base_url,
    )
    if status == 200:
        return True
    # "Admin already exists" surfaces as different (status, message)
    # pairs across Dokploy versions / code paths:
    #   - 400 'Admin is already created' (better-auth's own guard)
    #   - 422 'User already exists. Use another email.' (Dokploy's
    #          custom user-create path in v0.29.x)
    # Both mean the same thing: this email is taken, caller should
    # try signin. Any 4xx with 'already' in the message is treated as
    # existence-not-an-error so we don't need a brittle status+message
    # whitelist per Dokploy release.
    msg = str(body.get("message", "")).lower()
    if 400 <= status < 500 and "already" in msg:
        return False
    raise RuntimeError(
        f"Dokploy signup failed: HTTP {status} {body.get('message') or body!r}"
    )


def better_auth_signin(base_url: str, email: str, password: str
                       ) -> str:
    """POST /api/auth/sign-in/email.

    Returns:
      The session cookie header value ("better-auth.session_token=...")
      suitable to pass as Cookie: on subsequent requests.
    Raises:
      DokployUnreachable          -- TCP/connection error.
      AdminCredentialsMismatch    -- 401: admin exists but this password
                                    is wrong. Caller falls back to
                                    the manual-key prompt.
      RuntimeError                -- any other non-success.
    """
    status, body, headers = _post_json(
        f"{base_url}/api/auth/sign-in/email",
        {"email": email, "password": password},
        origin=base_url,
    )
    if status == 401:
        raise AdminCredentialsMismatch(
            "Dokploy signin 401: admin exists but vault_admin_password "
            "does not match. The operator probably created the admin "
            "through the UI with a different password, or rotated it "
            "after install."
        )
    if status != 200:
        raise RuntimeError(
            f"Dokploy signin failed: HTTP {status} "
            f"{body.get('message') or body!r}"
        )
    # Find the session cookie. urllib lowercases header names; better-auth
    # sets several Set-Cookie lines but the Python client concatenates
    # them with a comma. Parse defensively.
    raw_cookies = headers.get("Set-Cookie") or headers.get("set-cookie") or ""
    # The Set-Cookie value looks like:
    #   better-auth.session_token=eyJ...; Path=/; HttpOnly; SameSite=Lax
    # We only need `<name>=<value>` for the request Cookie header.
    for candidate in raw_cookies.split(","):
        piece = candidate.strip().split(";", 1)[0].strip()
        if piece.startswith(f"{SESSION_COOKIE_NAME}="):
            return piece
    raise RuntimeError(
        f"Dokploy signin OK but no {SESSION_COOKIE_NAME} cookie in "
        f"response. Headers: {headers!r}"
    )


def better_auth_list_organizations(base_url: str, cookie: str) -> list[dict]:
    """GET /api/auth/organization/list with session cookie. Returns
    the list of organizations the signed-in user is a member of.

    Dokploy's signup `after` hook creates one organization ("My
    Organization") per admin at sign-up time, so on a freshly
    bootstrapped install this list has exactly one entry. We use its
    id as the api-key metadata's organizationId -- Dokploy's
    validateRequest rejects keys whose metadata lacks that field."""
    status, body, _ = _get_json(
        f"{base_url}/api/auth/organization/list",
        cookie=cookie,
        origin=base_url,
    )
    if status != 200:
        raise RuntimeError(
            f"Dokploy organization/list failed: HTTP {status} "
            f"{(body.get('message') if isinstance(body, dict) else None) or body!r}"
        )
    if not isinstance(body, list):
        raise RuntimeError(
            f"Dokploy organization/list returned non-list: {body!r}"
        )
    return body


def dokploy_create_api_key(base_url: str, cookie: str,
                           organization_id: str,
                           name: str = API_KEY_LABEL) -> str:
    """POST /api/user.createApiKey (Dokploy's own tRPC wrapper around
    auth.createApiKey) with session cookie. Returns the minted key
    string (body["key"]).

    Why NOT the better-auth endpoint /api/auth/api-key/create:
      - That endpoint is gated by better-auth's "server-only input"
        guard on rateLimitEnabled / rateLimitMax / remaining -- any
        client request setting those gets:
          HTTP 400 'The property you're trying to set can only be set
                    from the server auth instance only.'
      - With rate-limiting at its plugin default (~10 req/min per
        key), a converge blows through the budget mid-run and every
        subsequent x-api-key call 401s until the window rolls.

    Dokploy's own procedure (apps/dokploy/server/api/routers/user.ts)
    is a protectedProcedure that wraps auth.createApiKey server-side
    and passes rateLimitEnabled through -- bypassing the client guard
    while still enforcing org-membership checks on the caller.

    `organization_id` is required (Dokploy's handler rejects with
    FORBIDDEN if the caller isn't a member) and lands in metadata so
    validateRequest can resolve the key to a member at use time.
    """
    if not organization_id:
        raise RuntimeError(
            "dokploy_create_api_key requires organization_id; Dokploy "
            "rejects keys whose metadata has no organizationId with 401."
        )
    status, body, _ = _post_json(
        f"{base_url}/api/user.createApiKey",
        {
            "name": name,
            # Dokploy's wrapper updates the apikey row's metadata column
            # with JSON.stringify(input.metadata) AFTER auth.createApiKey
            # returns (see packages/server/src/services/user.ts).
            "metadata": {"organizationId": organization_id},
            # The whole reason for using this endpoint. Disables
            # better-auth's per-key rate limit (default ~10/min) so a
            # long converge doesn't start 401ing partway through.
            "rateLimitEnabled": False,
        },
        cookie=cookie,
        origin=base_url,
    )
    if status != 200:
        raise RuntimeError(
            f"Dokploy user.createApiKey failed: HTTP {status} "
            f"{body.get('message') or body!r}"
        )
    key = body.get("key")
    if not isinstance(key, str) or not key:
        raise RuntimeError(
            f"Dokploy user.createApiKey OK but no 'key' in response body: "
            f"{body!r}"
        )
    return key


# --- reachability probe ----------------------------------------------------
def wait_for_reachable(base_url: str,
                       timeout_s: float = REACHABILITY_TIMEOUT_S,
                       poll_s: float = REACHABILITY_POLL_S,
                       clock=time.monotonic,
                       sleep=time.sleep) -> bool:
    """Poll `base_url` until ANY HTTP response comes back (even 4xx/5xx
    proves the server is up). Returns True on success, False if the
    timeout elapses without a single answered request.

    Why not bail on first ConnectionRefused: site.yml's `dokploy` role
    completes as soon as the compose-create call returns, but the
    server container still has 20-40s of Drizzle migrations + Next.js
    startup ahead. During that window we get ECONNREFUSED. Retry.

    `clock` + `sleep` are injectable for deterministic tests (the
    defaults tie this to real wall time)."""
    deadline = clock() + timeout_s
    attempts = 0
    last_err: str = ""
    while True:
        attempts += 1
        try:
            with urllib.request.urlopen(
                urllib.request.Request(base_url, method="GET"),
                timeout=HTTP_TIMEOUT,
            ) as r:
                # Any status is proof-of-life; we don't care if it's 200.
                _ = r.status
                return True
        except urllib.error.HTTPError:
            # 4xx/5xx means the server is up and answering.
            return True
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last_err = str(e)
        if clock() >= deadline:
            _warn(
                f"Dokploy not reachable at {base_url} after "
                f"{int(timeout_s)}s / {attempts} attempts; last error: "
                f"{last_err}"
            )
            return False
        sleep(poll_s)


# --- orchestration ---------------------------------------------------------
def bootstrap(base_url: str, email: str, password: str,
              reachability_timeout_s: float = REACHABILITY_TIMEOUT_S) -> str:
    """Full flow: wait for reachable -> signup (or skip if admin
    exists) -> signin -> create API key. Returns the minted key.
    Raises DokployUnreachable / AdminCredentialsMismatch /
    RuntimeError as appropriate."""
    _info(f"waiting for Dokploy at {base_url} (up to "
          f"{int(reachability_timeout_s)}s)")
    if not wait_for_reachable(base_url, timeout_s=reachability_timeout_s):
        raise DokployUnreachable(
            f"Dokploy did not respond on {base_url} within "
            f"{int(reachability_timeout_s)}s."
        )
    _ok("Dokploy is answering")

    _info(f"signup {email} at {base_url}")
    created = better_auth_signup(base_url, email, password)
    if created:
        _ok("admin account created")
    else:
        _info("admin already exists; signing in with vault_admin_password")

    _info("sign-in")
    cookie = better_auth_signin(base_url, email, password)
    _ok("session established")

    _info("resolving organization id")
    orgs = better_auth_list_organizations(base_url, cookie)
    if not orgs:
        raise RuntimeError(
            "Dokploy organization/list returned an empty list -- the "
            "signup after-hook should have created 'My Organization' "
            "but didn't. This is a Dokploy-side state issue; manual "
            "recovery via the UI may be needed."
        )
    organization_id = orgs[0].get("id")
    if not isinstance(organization_id, str) or not organization_id:
        raise RuntimeError(
            f"first organization has no usable id: {orgs[0]!r}"
        )
    _ok(f"organization {organization_id} (of {len(orgs)} total)")

    _info(f"minting API key (name={API_KEY_LABEL!r})")
    api_key = dokploy_create_api_key(base_url, cookie, organization_id)
    _ok(f"API key minted ({len(api_key)} chars)")
    return api_key


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--vault", type=Path,
                    help="SOPS-encrypted vault that will RECEIVE the minted "
                         "vault_dokploy_api_key (vault.sops.yml). Required "
                         "unless --emit-only is set.")
    ap.add_argument("--admin-password-vault", type=Path,
                    help="SOPS-encrypted vault holding vault_admin_password. "
                         "Defaults to --vault.")
    ap.add_argument("--env-file", required=True, type=Path,
                    help="Path to inventory/<name>/.env (for ADMIN_EMAIL).")
    ap.add_argument("--tailnet-ip", required=True,
                    help="Tailnet IPv4 of the Dokploy host.")
    ap.add_argument("--port", type=int, default=3000,
                    help="Dokploy API port on the tailnet (default 3000).")
    ap.add_argument("--admin-email",
                    help="Override ADMIN_EMAIL lookup in .env.")
    ap.add_argument("--emit-only", action="store_true",
                    help="Skip the in-place merge into --vault and emit the "
                         "minted key on stdout only. Used by fresh_install.yml "
                         "post-Option-1: persistence is the caller's job (via "
                         "helpers/bootstrap_output.py + operator PR-back into "
                         "catenahq/inventories), not this helper's. --vault "
                         "is not required when --emit-only is set.")
    args = ap.parse_args(argv)

    if not args.emit_only:
        if args.vault is None:
            print("--vault is required unless --emit-only is set",
                  file=sys.stderr)
            return EXIT_ERROR
        if not args.vault.is_file():
            print(f"vault not found: {args.vault}", file=sys.stderr)
            return EXIT_ERROR
    admin_pw_vault = args.admin_password_vault or args.vault
    if admin_pw_vault is None:
        print("--admin-password-vault is required when --vault is not given",
              file=sys.stderr)
        return EXIT_ERROR
    if not admin_pw_vault.is_file():
        print(f"admin-password vault not found: {admin_pw_vault}", file=sys.stderr)
        return EXIT_ERROR

    try:
        password = sops_vault.read_value(admin_pw_vault, "vault_admin_password")
    except sops_vault.SopsError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_ERROR
    if not password:
        _warn("vault_admin_password not in vault -- can't auto-bootstrap. "
              "Falling back to manual API key prompt.")
        return EXIT_ERROR

    email = (args.admin_email
             or read_env_value(args.env_file, "ADMIN_EMAIL")).strip()
    if not email:
        _warn("ADMIN_EMAIL not in .env and not given as --admin-email. "
              "Falling back to manual API key prompt.")
        return EXIT_ERROR

    base_url = f"http://{args.tailnet_ip}:{args.port}"
    print(
        "\n\033[1;34m== Dokploy admin + API key (auto-bootstrap via better-auth)\033[0m",
        file=sys.stderr,
    )

    try:
        api_key = bootstrap(base_url, email, password)
    except DokployUnreachable as exc:
        _warn(f"Dokploy unreachable at {base_url}: {exc}. Falling back "
              "to manual prompt.")
        return EXIT_UNREACHABLE
    except AdminCredentialsMismatch as exc:
        _warn(str(exc))
        _warn("Falling back to manual API key prompt.")
        return EXIT_ADMIN_MISMATCH
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_ERROR

    if not args.emit_only:
        try:
            sops_vault.set_value(args.vault, "vault_dokploy_api_key", api_key)
        except sops_vault.SopsError as exc:
            print(str(exc), file=sys.stderr)
            return EXIT_ERROR
        _ok(f"merged vault_dokploy_api_key into {args.vault}")
    else:
        _ok("--emit-only: skipped vault merge; caller persists via "
            "helpers/bootstrap_output.py + operator PR")
    # Emit the minted key on stdout so the caller (fresh_install.yml's
    # between-converges localhost play) can `register:` it and:
    #   1. inject it into the target host's runtime vars via add_host
    #      (in-memory bridge for the next site.yml import in the same
    #      ansible-playbook invocation -- the community.sops vars plugin
    #      caches decrypted vault contents per-process, so a vault
    #      re-read would not see a freshly-merged value);
    #   2. merge it into <inventory_dir>/.bootstrap-output.yml via
    #      helpers/bootstrap_output.py for the operator to PR into
    #      catenahq/inventories ahead of subsequent invocations.
    # All status messages go to stderr (see _ok/_info/_warn), so stdout
    # remains a single clean line containing only the secret. ansible's
    # `no_log: true` on the register'ing task keeps it out of logs.
    print(api_key)
    return EXIT_OK


def _entrypoint() -> int:
    try:
        return main()
    except KeyboardInterrupt:
        print("\nCancelled (interrupted).", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(_entrypoint())
