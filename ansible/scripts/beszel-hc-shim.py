#!/usr/bin/env python3
"""Beszel -> Healthchecks alert shim.

Beszel notifies via Shoutrrr with a STATIC URL per channel and cannot
branch triggered-vs-resolved onto different endpoints the way Gatus's
[ALERT_TRIGGERED_OR_RESOLVED] placeholder does. This sidecar bridges the
gap so Beszel threshold alerts ride the same Healthchecks notification
plane as Gatus (clients keep their self-service channels).

Beszel sends a Shoutrrr "generic" webhook -- a JSON POST with `title`
and `message` (henrygd/beszel internal/alerts/alerts.go SendShoutrrrAlert
adds the title as a JSON field for the generic scheme). The title fully
encodes what we need:

  status alert : "Connection to <system> is up|down <emoji>"
  metric alert : "<system> <metric> above|below threshold"

We derive a STABLE per-(system, metric) slug and a triggered/resolved
flag, then ping Healthchecks: resolved -> success ping (check flips UP),
triggered -> /fail ping (check flips DOWN + fans out to every project
channel). `?create=1` auto-provisions the beszel-<slug> check on first
fire, exactly like the Gatus path in notifications.md.

Runs inside a python:alpine container on dokploy-network. Config via env:

    HC_URL        Healthchecks base, e.g. http://healthchecks:8000
    HC_PING_KEY   the project ping key (vault_healthchecks_ping_key)
    LISTEN_PORT   port to listen on (default 8099)
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

_NON_SLUG = re.compile(r"[^a-z0-9]+")
_STATUS_RE = re.compile(r"^connection to (?P<system>.+) is (?P<state>up|down)\b")
_METRIC_RE = re.compile(r"^(?P<body>.+) (?P<dir>above|below) threshold$")
# Healthchecks slug regex is strict + lowercase; cap length defensively.
_SLUG_MAX = 60
PING_TIMEOUT = 10


def slugify(text: str) -> str:
    """Lowercase [a-z0-9-] slug, collapsed + trimmed, capped at _SLUG_MAX."""
    s = _NON_SLUG.sub("-", text.lower()).strip("-")
    return s[:_SLUG_MAX].strip("-")


def parse_alert(payload: dict) -> tuple[str, bool] | None:
    """Map a Beszel webhook payload to (slug, resolved).

    Returns None when the title is unrecognisable (caller drops it rather
    than ping a garbage slug). `resolved=True` -> system recovered.

    The slug must be identical for the triggered and resolved events of
    the same alert so both hit the same Healthchecks check -- so the
    state-varying token (up/down, above/below) is normalised OUT of the
    slug and only feeds the resolved flag.
    """
    title = (payload.get("title") or payload.get("Title") or "").strip()
    if not title:
        return None
    low = title.lower()

    m = _STATUS_RE.match(low)
    if m:
        resolved = m.group("state") == "up"
        return f"beszel-{slugify(m.group('system') + '-status')}", resolved

    m = _METRIC_RE.match(low)
    if m:
        # Normal high-metric alerts (CPU/memory/disk/temp/bandwidth/load):
        # above threshold = triggered, below threshold = resolved. Battery
        # (low alert) inverts this, but VPS hosts have no battery.
        resolved = m.group("dir") == "below"
        return f"beszel-{slugify(m.group('body'))}", resolved

    return None


def build_ping_url(base: str, key: str, slug: str, resolved: bool) -> str:
    """Healthchecks ping URL: success on resolved, /fail on triggered."""
    suffix = "" if resolved else "/fail"
    return f"{base.rstrip('/')}/ping/{key}/{slug}{suffix}?create=1"


def _read_payload(raw: bytes) -> dict:
    try:
        obj = json.loads(raw.decode() or "{}")
        return obj if isinstance(obj, dict) else {}
    except (ValueError, UnicodeDecodeError):
        return {}


class _Handler(BaseHTTPRequestHandler):
    # Injected by main().
    hc_url = ""
    hc_ping_key = ""

    def log_message(self, *args):  # noqa: D401 - quiet the default stderr spam
        pass

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length") or 0)
        payload = _read_payload(self.rfile.read(length) if length else b"")
        parsed = parse_alert(payload)
        if parsed is None:
            self.send_response(204)
            self.end_headers()
            return
        slug, resolved = parsed
        url = build_ping_url(self.hc_url, self.hc_ping_key, slug, resolved)
        try:
            urllib.request.urlopen(url, timeout=PING_TIMEOUT).read()
            self.send_response(200)
        except Exception as exc:  # noqa: BLE001 - never crash the listener
            sys.stderr.write(f"beszel-hc-shim: ping failed for {slug}: {exc}\n")
            self.send_response(502)
        self.end_headers()


def main() -> int:
    _Handler.hc_url = os.environ["HC_URL"]
    _Handler.hc_ping_key = os.environ["HC_PING_KEY"]
    port = int(os.environ.get("LISTEN_PORT", "8099"))
    HTTPServer(("0.0.0.0", port), _Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
