"""Ansible filter: the SECONDARY domains this host currently serves.

Reads the projection the Cloudflare tunnel engine writes after a successful
converge (/var/lib/catena/cloudflare-zones.json): the ordered list of domains
that converge actually brought up, element 0 being the primary. This role needs
the ones after it, to route an auth.<zone> host rule per attached domain.

    catena_served_zones(raw, schema=1, now=<iso8601>, max_age_hours=48)
        -> ["second.com", "third.com"]   (primary dropped, order preserved)

Every reason it cannot answer collapses to the SAME safe result, an empty list,
because every one of them means the same thing to the caller: route the primary
domain only.

  - the file is absent      a single-domain host, or a converge that has not
                            reached the tunnel role yet
  - the file is malformed   never guess at a partial write
  - an unknown schema       a newer writer; refuse rather than mis-parse
  - the stamp is too old    the engine has not converged in a long time, so the
                            list may name a domain the edge no longer serves.
                            Routing to one of those sends users to a hostname
                            that resolves nowhere.

The staleness bound is what makes this safe to trust at all: an unstamped list
(the shape this replaced) was indistinguishable fresh from months old.

This repo deliberately knows nothing about subscriptions. The projection holds
only what the engine was entitled to converge, so a lapse degrades the routing
to primary-only on its own.

End-to-end coverage:
    ansible/tests/unit/test_catena_served_zones.py
"""
from __future__ import annotations

import json
from datetime import datetime, timezone


def _parse_iso(value):
    """Parse an ISO-8601 stamp into an aware datetime, or None. Accepts the
    trailing Z ansible_date_time.iso8601 and Go's time.Time both emit."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def catena_served_zones(raw, schema=1, now="", max_age_hours=48):
    """Return the secondary domain names from a zone projection."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    if not isinstance(raw, str) or not raw.strip():
        return []
    try:
        doc = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(doc, dict):
        # The legacy shape was a bare array. Refused on purpose: it carried no
        # stamp, so a stale copy could not be told from a current one.
        return []
    if doc.get("schema") != schema:
        return []

    zones = doc.get("zones")
    if not isinstance(zones, list) or not zones:
        return []

    try:
        max_age = float(max_age_hours)
    except (TypeError, ValueError):
        max_age = 0.0
    if max_age > 0:
        generated = _parse_iso(doc.get("generated_at"))
        reference = _parse_iso(now)
        if generated is None:
            return []
        if reference is not None:
            age_hours = (reference - generated).total_seconds() / 3600.0
            if age_hours > max_age:
                return []

    out = []
    for entry in zones[1:]:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("zone") or "").strip()
        if name and name not in out:
            out.append(name)
    return out


class FilterModule:
    def filters(self):
        return {"catena_served_zones": catena_served_zones}
