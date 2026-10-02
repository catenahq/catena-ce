#!/usr/bin/env python3
"""catena-tailnet-check -- check tailnet credentials before the panel stores them.

Driven by catena-admin over the host dispatcher (as root), on a Settings save
that changes the private network: the reserved action pipes the request here
on stdin, the same way the settings write that follows it travels. Nothing is
stored and nothing joins; the join runs after the save.

Request (JSON on stdin), each value the one the save would leave in the store:
  {"TAILNET_PROVIDER": "tailscale" | "headscale",
   "TAILSCALE_TAGS": "tag:vps,...",
   "tailscale_oauth_client_id": "...", "tailscale_oauth_client_secret": "...",
   "TAILNET_CONTROL_URL": "https://...", "HEADSCALE_USER": "...",
   "headscale_api_key": "...", "headscale_preauth_key": "..."}

Tailscale: the OAuth client exchanges for a token, mints a 60-second
single-use key under the tags (the join mints its key the same way), and reads
the device list (the Lockdown asks it whether this server is connected before
it closes public SSH). Headscale: the server answers, and an API key lists its
users, the one named included; a pre-auth key alone is only proven by the join.

Prints {"valid": bool, "reason": str} and exits 0 when valid, 1 otherwise.
Nothing it prints carries a credential. stdlib only.
"""
from __future__ import annotations

import base64
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

TAILSCALE_API = "https://api.tailscale.com/api/v2"

Http = Callable[..., tuple[int, object]]


def _http(method: str, url: str, *, headers: dict | None = None,
          data: bytes | None = None) -> tuple[int, object]:
    """(status, parsed JSON or None). Status 0 means no answer arrived."""
    req = urllib.request.Request(url, method=method, data=data,
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310
            body = r.read()
            status = r.status
    except urllib.error.HTTPError as e:
        return e.code, None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return 0, None
    try:
        return status, json.loads(body or b"null")
    except ValueError:
        return status, None


def _tags(req: dict) -> list[str]:
    return [t.strip() for t in str(req.get("TAILSCALE_TAGS") or "").split(",")
            if t.strip()]


def check_tailscale(req: dict, http: Http = _http) -> str:
    """The reason the credentials would fail the join or the Lockdown, or ""."""
    client_id = str(req.get("tailscale_oauth_client_id") or "").strip()
    secret = str(req.get("tailscale_oauth_client_secret") or "").strip()
    if not (client_id and secret):
        return "the Tailscale OAuth client ID and secret are both required"
    tags = _tags(req)
    if not tags:
        return ("the private network tags are empty; the server joins under "
                "them, for example tag:vps")
    auth = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
    code, token = http(
        "POST", f"{TAILSCALE_API}/oauth/token",
        headers={"Authorization": f"Basic {auth}",
                 "Content-Type": "application/x-www-form-urlencoded"},
        data=urllib.parse.urlencode({"grant_type": "client_credentials"}).encode())
    if code != 200 or not isinstance(token, dict) or not token.get("access_token"):
        return (f"Tailscale refused the OAuth client (HTTP {code}); check the "
                f"client ID and secret")
    bearer = {"Authorization": f"Bearer {token['access_token']}"}
    key = {"capabilities": {"devices": {"create": {
        "reusable": False, "ephemeral": True, "preauthorized": True,
        "tags": tags}}}, "expirySeconds": 60}
    code, _ = http("POST", f"{TAILSCALE_API}/tailnet/-/keys",
                   headers={**bearer, "Content-Type": "application/json"},
                   data=json.dumps(key).encode())
    if code != 200:
        return (f"the OAuth client cannot create keys tagged {', '.join(tags)} "
                f"(HTTP {code}): give it the Auth Keys write scope and those "
                f"tags, and declare the tags in the tailnet policy")
    code, _ = http("GET", f"{TAILSCALE_API}/tailnet/-/devices", headers=bearer)
    if code != 200:
        return (f"the OAuth client cannot read devices (HTTP {code}): the "
                f"Lockdown asks Tailscale whether this server is connected "
                f"before it closes public SSH, which needs the Devices > Core "
                f"read scope")
    return ""


def check_headscale(req: dict, http: Http = _http) -> str:
    """The reason the settings would fail the join, or ""."""
    url = str(req.get("TAILNET_CONTROL_URL") or "").strip().rstrip("/")
    if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
        return "the Headscale server address must start with https://"
    api_key = str(req.get("headscale_api_key") or "").strip()
    preauth = str(req.get("headscale_preauth_key") or "").strip()
    if not (api_key or preauth):
        return "a Headscale API key or pre-authentication key is required"
    if not api_key:
        code, _ = http("GET", f"{url}/health")
        if code == 0:
            return f"the Headscale server at {url} does not answer"
        return ""
    user = str(req.get("HEADSCALE_USER") or "").strip()
    if not user:
        return ("the Headscale user is required: the server's key is minted "
                "for that user")
    code, body = http("GET", f"{url}/api/v1/user",
                      headers={"Authorization": f"Bearer {api_key}"})
    if code == 0:
        return f"the Headscale server at {url} does not answer"
    if code != 200 or not isinstance(body, dict):
        return f"the Headscale server refused the API key (HTTP {code})"
    names = {str(u.get("name") or "") for u in body.get("users") or []}
    if user not in names:
        return f"the Headscale server has no user named {user}"
    return ""


def check(req: dict, http: Http = _http) -> dict:
    provider = str(req.get("TAILNET_PROVIDER") or "").strip().lower()
    if provider == "tailscale":
        reason = check_tailscale(req, http)
    elif provider == "headscale":
        reason = check_headscale(req, http)
    else:
        reason = f"unknown tailnet provider {provider!r}"
    return {"valid": not reason, "reason": reason}


def main() -> int:
    try:
        req = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        req = None
    if not isinstance(req, dict):
        result = {"valid": False, "reason": "the request is not a JSON object"}
    else:
        result = check(req)
    print(json.dumps(result))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    sys.exit(main())
