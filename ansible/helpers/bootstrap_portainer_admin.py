#!/usr/bin/env python3
"""Leaf utility: mint a Portainer X-API-Key from the initial admin.

Portainer CE is the container control plane. Its admin is ALREADY created
at container first boot via `--admin-password-file` (roles/portainer stages
vault_admin_password into a swarm secret and passes the file to the
portainer binary). So there is no signup step here -- we only sign in and
mint a long-lived API token.

The flow (verified endpoint map: roles/portainer/README.md):

  POST /api/auth                 {Username, Password}
    -> 200 {"jwt": "..."}        session JWT (Authorization: Bearer <jwt>)
    -> 401/422                    admin exists but this password is wrong.

  GET  /api/users                (Bearer JWT)
    -> [{Id, Username, Role}]    resolve the admin user's Id (Role==1).

  POST /api/users/{id}/tokens    (Bearer JWT) {description, password}
    -> 200 {"rawAPIKey": "..."}  the X-API-Key value (shown once). Portainer
                                 RE-verifies the password on this call.

This helper chains those calls with the shared admin password, which it
reads on STDIN -- the caller holds it, this process never touches a file
that stores it. On success it prints the minted rawAPIKey on stdout
(status messages go to stderr) and exits 0; persisting the key is the
caller's job. roles/portainer writes it into the on-box config store at
/etc/catena/config.json, which is the only place a Catena secret lives.

Contract:
  stdin        : the admin password, one line.
  stdout       : the minted rawAPIKey, on success only.
  Exit code 0  : API key minted.
  Exit code 2  : Portainer unreachable at tailnet:port -- caller should
                 fall back / retry. Not a hard failure; Portainer may
                 still be initialising its BoltDB store.
  Exit code 3  : Portainer reachable but the admin password does not match
                 (auth returned 401/422). The operator either changed the
                 admin password in the UI or rotated vault_admin_password
                 after install. We don't try to reset it.
  Exit code 1  : unexpected error (empty stdin, unexpected HTTP code,
                 malformed responses, etc.).

Usage:
    printf '%s' "$ADMIN_PASSWORD" | python3 \\
        helpers/bootstrap_portainer_admin.py \\
        --tailnet-ip 100.77.16.46 \\
        --port 9000
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_UNREACHABLE = 2
EXIT_ADMIN_MISMATCH = 3

# Portainer's initial admin username (fixed by --admin-password-file boot).
DEFAULT_ADMIN_USER = "admin"

# Portainer's administrator role id (owner). Role 2 is standard user.
PORTAINER_ADMIN_ROLE = 1

# Label the token carries in Portainer's UI (Settings -> access tokens).
API_KEY_LABEL = "catena-installer"

HTTP_TIMEOUT = 10.0

# Reachability probe window. After site.yml deploys roles/portainer, the
# service task may still be scheduling + initialising its BoltDB store.
# Block until the HTTP port answers anything (even a 4xx proves life).
REACHABILITY_TIMEOUT_S = 120
REACHABILITY_POLL_S = 3


def _warn(msg: str) -> None:
    print(f"\033[1;33m!\033[0m {msg}", file=sys.stderr)


def _ok(msg: str) -> None:
    print(f"\033[1;32m+\033[0m {msg}", file=sys.stderr)


def _info(msg: str) -> None:
    print(f"\033[1;34m-\033[0m {msg}", file=sys.stderr)


# --- HTTP primitives -------------------------------------------------------
class PortainerUnreachable(RuntimeError):
    """Thrown when the Portainer base URL can't be reached at all."""


class AdminCredentialsMismatch(RuntimeError):
    """Thrown when /api/auth rejects vault_admin_password (401/422)."""


def _request(url: str, *, method: str, body: dict | None = None,
             token: str | None = None) -> tuple[int, object]:
    """Issue a JSON request. Returns (status, parsed-body). Parsed body is
    whatever json.loads yields (dict OR list -- Portainer's /api/users
    returns a list). Raises PortainerUnreachable on connection failures."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"accept": "application/json"}
    if data is not None:
        headers["content-type"] = "application/json"
    if token:
        headers["authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            return r.status, _parse(r.read())
    except urllib.error.HTTPError as e:
        return e.code, _parse(e.read())
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise PortainerUnreachable(str(e)) from e


def _parse(raw: bytes):
    try:
        return json.loads(raw.decode("utf-8") or "null")
    except (ValueError, UnicodeDecodeError):
        return None


def _msg(body: object) -> str:
    """Best-effort human message from a Portainer error body."""
    if isinstance(body, dict):
        return str(body.get("message") or body.get("details") or body)
    return repr(body)


def portainer_signin(base_url: str, user: str, password: str) -> str:
    """POST /api/auth -> session JWT. Raises AdminCredentialsMismatch on
    401/422 (bad password), PortainerUnreachable on TCP error, RuntimeError
    on any other non-200."""
    status, body = _request(
        f"{base_url}/api/auth", method="POST",
        body={"Username": user, "Password": password},
    )
    if status in (401, 422):
        raise AdminCredentialsMismatch(
            f"Portainer /api/auth {status}: admin '{user}' exists but "
            "vault_admin_password does not match. The operator probably "
            "changed the admin password in the UI, or rotated "
            "vault_admin_password after install."
        )
    if status != 200:
        raise RuntimeError(f"Portainer signin failed: HTTP {status} {_msg(body)}")
    jwt = body.get("jwt") if isinstance(body, dict) else None
    if not isinstance(jwt, str) or not jwt:
        raise RuntimeError(f"Portainer signin OK but no jwt in body: {body!r}")
    return jwt


def portainer_admin_user_id(base_url: str, jwt: str, user: str) -> int:
    """GET /api/users -> the Id of the admin user (Username match, or the
    first Role==1 administrator as a fallback)."""
    status, body = _request(f"{base_url}/api/users", method="GET", token=jwt)
    if status != 200:
        raise RuntimeError(f"Portainer /api/users failed: HTTP {status} {_msg(body)}")
    if not isinstance(body, list):
        raise RuntimeError(f"Portainer /api/users returned non-list: {body!r}")
    by_name = [u for u in body if isinstance(u, dict) and u.get("Username") == user]
    admins = [u for u in body if isinstance(u, dict)
              and u.get("Role") == PORTAINER_ADMIN_ROLE]
    chosen = (by_name or admins or [None])[0]
    if not isinstance(chosen, dict) or not isinstance(chosen.get("Id"), int):
        raise RuntimeError(
            f"Portainer /api/users has no admin user (looked for "
            f"Username={user!r} or Role={PORTAINER_ADMIN_ROLE}): {body!r}"
        )
    return chosen["Id"]


def portainer_create_api_key(base_url: str, jwt: str, user_id: int,
                             password: str, name: str = API_KEY_LABEL) -> str:
    """POST /api/users/{id}/tokens -> rawAPIKey. Portainer re-verifies the
    password on this call, so a mismatch surfaces here too as 401/403."""
    status, body = _request(
        f"{base_url}/api/users/{user_id}/tokens", method="POST",
        body={"description": name, "password": password}, token=jwt,
    )
    if status in (401, 403):
        raise AdminCredentialsMismatch(
            f"Portainer token create {status}: password re-check failed for "
            f"user id {user_id}."
        )
    if status not in (200, 201):
        raise RuntimeError(
            f"Portainer token create failed: HTTP {status} {_msg(body)}"
        )
    key = body.get("rawAPIKey") if isinstance(body, dict) else None
    if not isinstance(key, str) or not key:
        raise RuntimeError(
            f"Portainer token create OK but no rawAPIKey in body: {body!r}"
        )
    return key


# --- reachability probe ----------------------------------------------------
def wait_for_reachable(base_url: str,
                       timeout_s: float = REACHABILITY_TIMEOUT_S,
                       poll_s: float = REACHABILITY_POLL_S,
                       clock=time.monotonic,
                       sleep=time.sleep) -> bool:
    """Poll base_url until ANY HTTP response comes back (even 4xx/5xx proves
    Portainer is up). Returns True on success, False on timeout. `clock` +
    `sleep` are injectable for deterministic tests."""
    deadline = clock() + timeout_s
    attempts = 0
    last_err = ""
    while True:
        attempts += 1
        try:
            with urllib.request.urlopen(
                urllib.request.Request(base_url, method="GET"),
                timeout=HTTP_TIMEOUT,
            ) as r:
                _ = r.status
                return True
        except urllib.error.HTTPError:
            return True
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last_err = str(e)
        if clock() >= deadline:
            _warn(f"Portainer not reachable at {base_url} after "
                  f"{int(timeout_s)}s / {attempts} attempts; last error: "
                  f"{last_err}")
            return False
        sleep(poll_s)


# --- orchestration ---------------------------------------------------------
def bootstrap(base_url: str, user: str, password: str,
              reachability_timeout_s: float = REACHABILITY_TIMEOUT_S) -> str:
    """Full flow: wait for reachable -> signin -> resolve admin id -> mint
    token. Returns the minted rawAPIKey."""
    _info(f"waiting for Portainer at {base_url} (up to "
          f"{int(reachability_timeout_s)}s)")
    if not wait_for_reachable(base_url, timeout_s=reachability_timeout_s):
        raise PortainerUnreachable(
            f"Portainer did not respond on {base_url} within "
            f"{int(reachability_timeout_s)}s."
        )
    _ok("Portainer is answering")

    _info(f"sign-in as {user!r}")
    jwt = portainer_signin(base_url, user, password)
    _ok("session established")

    _info("resolving admin user id")
    user_id = portainer_admin_user_id(base_url, jwt, user)
    _ok(f"admin user id {user_id}")

    _info(f"minting API key (name={API_KEY_LABEL!r})")
    api_key = portainer_create_api_key(base_url, jwt, user_id, password)
    _ok(f"API key minted ({len(api_key)} chars)")
    return api_key


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--tailnet-ip", required=True,
                    help="Tailnet IPv4 of the Portainer host.")
    ap.add_argument("--port", type=int, default=9000,
                    help="Portainer UI/API port on the tailnet (default 9000).")
    ap.add_argument("--admin-user", default=DEFAULT_ADMIN_USER,
                    help=f"Portainer admin username (default "
                         f"{DEFAULT_ADMIN_USER!r}).")
    args = ap.parse_args(argv)

    # Trailing newline only -- a Portainer password may legitimately end in
    # whitespace, and .strip() would silently mint against a different one.
    password = sys.stdin.read().rstrip("\r\n")
    if not password:
        _warn("no admin password on stdin -- can't mint a Portainer API key.")
        return EXIT_ERROR

    base_url = f"http://{args.tailnet_ip}:{args.port}"
    print(
        "\n\033[1;34m== Portainer API key (mint from initial admin)\033[0m",
        file=sys.stderr,
    )

    try:
        api_key = bootstrap(base_url, args.admin_user, password)
    except PortainerUnreachable as exc:
        _warn(f"Portainer unreachable at {base_url}: {exc}.")
        return EXIT_UNREACHABLE
    except AdminCredentialsMismatch as exc:
        _warn(str(exc))
        return EXIT_ADMIN_MISMATCH
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_ERROR

    # Emit the key on stdout (status messages go to stderr) so a caller can
    # register: it; ansible's no_log keeps it out of logs.
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
