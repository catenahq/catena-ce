#!/usr/bin/env python3
"""Ask the tailnet whether this host is reachable on it, before public 22 closes.

Runs ON the host, shipped by bootstrap/roles/tailscale/tasks/reachable.yml,
which playbooks/lockdown.yml runs before it closes public 22. Each question is
answered by something other than the host's own view of itself:

  1. The control server's record of this node. Tailscale: the device API's
     `connectedToControl` and `blocksIncomingConnections`. Headscale: the node
     API's `online`. A node the control server does not see connected, or one
     that refuses incoming connections, is reachable by nobody.
  2. With TAILNET_REQUIRE_PEER=1 (what reachable.yml sets): an online peer that
     carries none of this host's tags answers a TSMP ping through the tunnel.

Input is the environment, so no credential reaches argv or the process table:
TAILNET_PROVIDER (tailscale | headscale), TAILSCALE_API_BASE,
TAILSCALE_OAUTH_CLIENT_ID, TAILSCALE_OAUTH_CLIENT_SECRET, HEADSCALE_URL,
HEADSCALE_API_KEY, TAILNET_OWN_TAGS (comma-separated), TAILNET_REQUIRE_PEER.

Prints one JSON object on stdout, {"ok", "reasons", "summary"}, and exits 0
whatever the verdict; the caller asserts on it. Nothing it prints carries a
credential.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

# A node that has just run `tailscale up` reaches the control server, and its
# netmap its peers, a few seconds later.
ATTEMPTS = 7
DELAY_S = 10.0
# At most this many peers are pinged per attempt.
MAX_PEERS = 5

Run = Callable[[list[str]], "subprocess.CompletedProcess[str]"]
Http = Callable[..., tuple[int, object]]


def _run(argv: list[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(argv, capture_output=True, text=True, timeout=60,
                          check=False)


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
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0, None
    try:
        return status, json.loads(body or b"null")
    except ValueError:
        return status, None


def local_status(run: Run) -> dict:
    r = run(["tailscale", "status", "--json"])
    if r.returncode != 0:
        raise RuntimeError(
            f"`tailscale status --json` exited {r.returncode}: "
            f"{(r.stderr or '').strip()[-300:]}")
    return json.loads(r.stdout)


def tailscale_control(status: dict, env: dict, http: Http) -> list[str]:
    """Reasons the Tailscale control server gives for this node being
    unreachable, or [] when it sees the node connected and accepting."""
    node_id = str((status.get("Self") or {}).get("ID") or "")
    if not node_id:
        return ["`tailscale status` names no node id for this host"]
    base = (env.get("TAILSCALE_API_BASE") or "https://api.tailscale.com/api/v2").rstrip("/")
    client_id = env.get("TAILSCALE_OAUTH_CLIENT_ID") or ""
    secret = env.get("TAILSCALE_OAUTH_CLIENT_SECRET") or ""
    if not (client_id and secret):
        return ["no Tailscale OAuth client is stored, so the control server "
                "cannot be asked"]
    auth = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
    code, token = http(
        "POST", f"{base}/oauth/token",
        headers={"Authorization": f"Basic {auth}",
                 "Content-Type": "application/x-www-form-urlencoded"},
        data=urllib.parse.urlencode({"grant_type": "client_credentials"}).encode())
    if code != 200 or not isinstance(token, dict) or not token.get("access_token"):
        return [f"the OAuth client did not exchange for a token (HTTP {code})"]
    code, device = http(
        "GET", f"{base}/device/{urllib.parse.quote(node_id)}?fields=all",
        headers={"Authorization": f"Bearer {token['access_token']}"})
    if code == 403:
        return ["the OAuth client cannot read devices: add Devices > Core "
                "(read) to its scopes in the Tailscale admin console"]
    if code != 200 or not isinstance(device, dict):
        return [f"the control server has no readable record of device "
                f"{node_id} (HTTP {code})"]
    reasons = []
    if not device.get("connectedToControl"):
        reasons.append("the control server does not see this server connected")
    if device.get("blocksIncomingConnections"):
        reasons.append("this server refuses incoming tailnet connections "
                       "(shields up), so no device can reach it")
    return reasons


def headscale_control(status: dict, env: dict, http: Http) -> list[str] | None:
    """Reasons the Headscale server gives, [] when it sees the node online,
    or None when no API key is stored to ask with."""
    key = env.get("HEADSCALE_API_KEY") or ""
    if not key:
        return None
    node_key = str((status.get("Self") or {}).get("PublicKey") or "")
    base = (env.get("HEADSCALE_URL") or "").rstrip("/")
    code, body = http("GET", f"{base}/api/v1/node",
                      headers={"Authorization": f"Bearer {key}"})
    if code != 200 or not isinstance(body, dict):
        return [f"the Headscale server did not list its nodes (HTTP {code})"]
    node = next((n for n in body.get("nodes") or []
                 if n.get("nodeKey") == node_key), None)
    if node is None:
        return ["the Headscale server has no node with this server's key"]
    if not node.get("online"):
        return ["the Headscale server does not see this server online"]
    return []


def eligible_peers(status: dict, own_tags: set[str]) -> list[tuple[str, str]]:
    """(name, address) of every online peer carrying none of this host's
    tags: a device an administrator could be on, not another server like
    this one."""
    out = []
    for peer in (status.get("Peer") or {}).values():
        if not peer.get("Online"):
            continue
        if own_tags & set(peer.get("Tags") or []):
            continue
        addrs = [a for a in peer.get("TailscaleIPs") or [] if "." in a]
        if addrs:
            out.append((peer.get("HostName") or addrs[0], addrs[0]))
    return out


def ping_a_peer(peers: list[tuple[str, str]], run: Run) -> str:
    """The name of the first peer that answers a TSMP ping, or ""."""
    for name, addr in peers[:MAX_PEERS]:
        r = run(["tailscale", "ping", "--tsmp", "--c", "3", "--timeout", "5s", addr])
        if r.returncode == 0:
            return name
    return ""


def check(env: dict, run: Run = _run, http: Http = _http,
          sleep: Callable[[float], None] = time.sleep) -> dict:
    provider = (env.get("TAILNET_PROVIDER") or "tailscale").strip().lower()
    require_peer = (env.get("TAILNET_REQUIRE_PEER") or "0").strip() == "1"
    own_tags = {t.strip() for t in (env.get("TAILNET_OWN_TAGS") or "").split(",")
                if t.strip()}

    reasons: list[str] = []
    notes: list[str] = []
    for attempt in range(ATTEMPTS):
        if attempt:
            sleep(DELAY_S)
        reasons, notes = [], []
        try:
            status = local_status(run)
        except (RuntimeError, ValueError) as e:
            reasons = [str(e)]
            continue
        if status.get("BackendState") != "Running":
            reasons = [f"tailscale is {status.get('BackendState')!r}, not Running"]
            continue

        if provider == "headscale":
            control = headscale_control(status, env, http)
            if control is None:
                if require_peer:
                    reasons.append(
                        "no Headscale API key is stored, so the control server "
                        "cannot confirm this server is online; enter one in "
                        "catena-admin > Settings > Access and tunnels")
                else:
                    notes.append("Headscale not asked (no API key stored)")
            else:
                reasons += control
        else:
            reasons += tailscale_control(status, env, http)

        if require_peer and not reasons:
            peers = eligible_peers(status, own_tags)
            if not peers:
                reasons.append(
                    "no other device is online on the tailnet: turn Tailscale "
                    "on on the device used to administer this server, then "
                    "apply again")
            else:
                answered = ping_a_peer(peers, run)
                if answered:
                    notes.append(f"{answered} answered through the tunnel")
                else:
                    reasons.append(
                        f"none of {', '.join(n for n, _ in peers[:MAX_PEERS])} "
                        f"answered a ping through the tunnel")
        if not reasons:
            break

    ok = not reasons
    summary = ("the tailnet confirms this server is reachable"
               + (f": {'; '.join(notes)}" if notes else "")) if ok else \
        "; ".join(reasons)
    return {"ok": ok, "reasons": reasons, "summary": summary}


def main() -> int:
    print(json.dumps(check(dict(os.environ))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
