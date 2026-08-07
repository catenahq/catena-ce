"""Intent-driven auth-gate verifier for validate (1ad).

The old validate-side probe enumerated `*-auto-gate.yml` files as its
only source, so an app whose gate file was REMOVED or renamed got zero
probes and validate passed green -- the exact stripped-gate regression
the probe exists to catch. This verifier derives the expected set from
the SAME source of truth the route writer uses
(gate_routes.derive_route_intent over the Portainer stack labels) and
then asserts, per routed app:

  1. the route file the writer would emit EXISTS in the Traefik
     dynamic dir (missing = route drift, FATAL);
  2. every GATED host denies an anonymous request (200 = auth bypass,
     FATAL; 30x redirect to Keycloak or a hard 401/403 = healthy);
  3. unreachable / odd statuses are FATAL too -- fail-closed, matching
     the static infra probe above it (the old probe warned only);
  4. any `*-auto-gate.yml` file in the dynamic dir that intent does
     NOT account for is stale (FATAL -- sync prunes them, so one
     lingering means sync has not run or is broken).

Probes run concurrently (stdlib ThreadPoolExecutor), collapsing the
old serial 10s-per-host worst case to ~one timeout total.

Runs on the host with the dashboard-sync env
(/etc/catena/dashboard-sync.env): PORTAINER_API_BASE,
PORTAINER_API_KEY, TRAEFIK_DYNAMIC_DIR, INFRA_COMPOSE_NAMES,
AUTH_HOSTNAME. Stdlib-only; installed beside dashboard-sync.

Output: one JSON report on stdout. Exit 0 = all clear, 1 = findings,
2 = usage/env error.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gate_routes  # noqa: E402
import route_synth  # noqa: E402

# Healthy anonymous outcomes on a GATED host: redirect to the IdP or a
# hard deny. Anything else is a finding (200 = bypass; the rest =
# routing problem, fail-closed).
HEALTHY_STATUSES = frozenset({301, 302, 303, 307, 308, 401, 403})
PROBE_TIMEOUT_S = 10


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def probe_anonymous(host: str, timeout: float = PROBE_TIMEOUT_S):
    """GET https://<host>/ without following redirects.

    Returns an int HTTP status, or a string error reason when no HTTP
    response came back at all (DNS, TLS, timeout)."""
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(
        f"https://{host}/", method="GET",
        headers={"User-Agent": "catena-validate-gate-probe"},
    )
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:  # noqa: BLE001 -- reason string is the payload
        return f"{type(e).__name__}: {e}"


def classify(intents: list[dict], dyn_dir: Path, statuses: dict) -> dict:
    """Pure classification of intent + filesystem + probe results into
    the finding sets. Split out so the unit suite can drive it without
    a Portainer or network."""
    report = {
        "missing_route_file": [],
        "bypass": [],
        "unreachable": [],
        "stale_route_file": [],
        "healthy": [],
        "public": [],
    }
    expected_fnames = set()
    for it in intents:
        expected_fnames.add(it["fname"])
        if not (dyn_dir / it["fname"]).is_file():
            report["missing_route_file"].append(
                {"app": it["name"], "host": it["host"], "fname": it["fname"]}
            )
        if it["is_public"]:
            report["public"].append(it["host"])
            continue
        status = statuses.get(it["host"])
        if status == 200:
            report["bypass"].append(it["host"])
        elif isinstance(status, int) and status in HEALTHY_STATUSES:
            report["healthy"].append(it["host"])
        else:
            report["unreachable"].append({"host": it["host"], "status": status})

    for path in sorted(dyn_dir.glob(f"*{route_synth.AUTO_ROUTE_SUFFIX}")):
        if path.name not in expected_fnames:
            report["stale_route_file"].append(path.name)
    return report


def findings(report: dict) -> list[str]:
    out = []
    for f in report["missing_route_file"]:
        out.append(
            f"route drift: {f['app']} ({f['host']}) has no {f['fname']} in "
            "the Traefik dynamic dir -- the auth gate is NOT routed"
        )
    for host in report["bypass"]:
        out.append(
            f"auth bypass: gated host {host} served HTTP 200 to an "
            "ANONYMOUS request"
        )
    for f in report["unreachable"]:
        out.append(
            f"fail-closed: gated host {f['host']} did not answer with an "
            f"auth redirect/deny (got {f['status']!r}) -- routing problem"
        )
    for fname in report["stale_route_file"]:
        out.append(
            f"stale route file: {fname} exists but no active stack "
            "accounts for it -- dashboard-sync prune has not run"
        )
    return out


def main() -> int:
    try:
        api_base = os.environ["PORTAINER_API_BASE"]
        api_key = os.environ["PORTAINER_API_KEY"]
        dyn_dir = Path(os.environ["TRAEFIK_DYNAMIC_DIR"])
        infra_names = os.environ["INFRA_COMPOSE_NAMES"]
        auth_hostname = os.environ["AUTH_HOSTNAME"]
    except KeyError as e:
        print(f"verify_gated_intent: missing env var {e}", file=sys.stderr)
        return 2

    skip_names = {n.strip() for n in infra_names.split(",") if n.strip()}
    intents = list(gate_routes.derive_route_intent(
        api_base, api_key, skip_names, auth_hostname,
    ))

    gated_hosts = [it["host"] for it in intents if not it["is_public"]]
    with ThreadPoolExecutor(max_workers=min(16, max(1, len(gated_hosts)))) as ex:
        statuses = dict(zip(gated_hosts, ex.map(probe_anonymous, gated_hosts)))

    report = classify(intents, dyn_dir, statuses)
    problems = findings(report)
    print(json.dumps({"report": report, "findings": problems}, indent=2))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
