#!/usr/bin/env python3
"""Seed Beszel's permanent universal token (idempotent).

Run on the host against the hub's loopback publish. Authenticates to
PocketBase as the superuser bootstrapped from USER_EMAIL/USER_PASSWORD,
looks up the matching users record, then find-or-updates the single
`universal_tokens` row (the collection has a unique index on `user`).

With a permanent universal token present, a Beszel agent that connects
with TOKEN=<that token> auto-registers its system on first WebSocket
connect (henrygd/beszel internal/hub/agent_connect.go
createNewSystemForUniversalToken) -- no manual "add system" step.

Config via env (mirrors the healthchecks-seed.py contract; KeyErrors
loudly on any wiring break):

    BESZEL_HUB_URL           e.g. http://127.0.0.1:18190
    BESZEL_ADMIN_EMAIL       superuser identity (== inventory admin_email)
    BESZEL_ADMIN_PASSWORD    superuser password (vault_beszel_admin_password)
    BESZEL_UNIVERSAL_TOKEN   the token to seed (vault_beszel_universal_token)

Exit 0 on success (prints one of: minted / updated / ok-exists). Any
failure exits non-zero with a stderr message so the calling Ansible
task's retry loop can wait out hub start-up.
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
        print("minted universal token")
        return 0
    row = rows[0]
    if row.get("token") == token:
        print("ok-exists")
        return 0
    _req(
        "PATCH",
        f"{base}/api/collections/universal_tokens/records/{row['id']}",
        token=su_token,
        body={"token": token},
    )
    print("updated universal token")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except urllib.error.HTTPError as exc:
        die(f"HTTP {exc.code} from hub API: {exc.read().decode(errors='replace')[:300]}")
    except urllib.error.URLError as exc:
        die(f"hub API unreachable: {exc}")
