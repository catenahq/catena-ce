"""Mailbox reconciler for the self-hosted mailserver template.

Provisions docker-mailserver mailboxes from Keycloak group membership, so a
mailbox exists for every staff/client user (docker-mailserver authenticates
via OAuth2 but does NOT create accounts -- the account must pre-exist or mail
is rejected).

Deliberately a SEPARATE module from dashboard-sync.py rather than another
concern bolted into that 750-line auth-critical monolith (see
BACKLOG_TECHNICAL.md "dashboard-sync.py decomposition"). dashboard-sync
imports `reconcile` and calls it non-fatally; a failure here never touches
the gate-route / redirect-URI provisioning path.

Design:
  - Gated on the `dms` container existing (the template is opt-in).
  - Desired set = email of every member of `/staff`, its department
    subgroups, and `/client` (the four-tier model: departments are
    subgroups of /staff).
  - Current set = `setup email list` inside dms.
  - ADD missing accounts (with a random throwaway password -- never used,
    never stored by us; OAuth2/XOAUTH2 is the real auth path, the password
    only satisfies DMS's account-existence requirement).
  - DELETE accounts whose domain we manage (appears in the desired set) but
    that are no longer desired -- so removing a user closes their mailbox,
    without ever touching addresses in domains we don't manage.

Stdlib-only. HTTP + subprocess are injected so the logic is unit-tested
without a network or docker.
"""

from __future__ import annotations

import re
import secrets
import subprocess

MANAGED_GROUPS = ("staff", "client")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# Local-parts that are provisioned outside Keycloak and must NEVER be
# reaped by the reconciler, even though they are not in the desired set.
# postmaster@ is bootstrapped as a real mailbox at converge
# (roles/infrastructure mailserver_accounts.yml) because docker-mailserver
# refuses to start Dovecot with zero mailboxes; deleting it would crash the
# server on the next restart. RFC 2142 also requires it to be deliverable.
PROTECTED_LOCALPARTS = frozenset({"postmaster"})


def _default_run(argv: list[str]) -> tuple[int, str]:
    p = subprocess.run(argv, capture_output=True, text=True)
    return p.returncode, p.stdout


def dms_container(run=_default_run) -> str:
    """Name of the running dms container, or '' if the template is not
    deployed."""
    rc, out = run([
        "docker", "ps", "--filter", "label=com.docker.compose.service=dms",
        "--format", "{{.Names}}",
    ])
    return out.splitlines()[0].strip() if rc == 0 and out.strip() else ""


# ─── Pure helpers (unit-tested without network/docker) ─────────────────────


def parse_account_list(text: str) -> set[str]:
    """Extract email addresses from `setup email list` output (DMS prints
    `* user@domain` lines plus occasional status text)."""
    return {m.group(0).lower() for line in text.splitlines()
            for m in [_EMAIL_RE.search(line)] if m}


def collect_member_emails(members: list[dict]) -> set[str]:
    """Lowercased emails from a Keycloak group-members representation,
    skipping members with no email."""
    out: set[str] = set()
    for m in members or []:
        email = (m.get("email") or "").strip().lower()
        if email:
            out.add(email)
    return out


def compute_actions(desired: set[str], current: set[str]) -> tuple[list[str], list[str]]:
    """(to_add, to_del). Deletions are scoped to domains we manage (a domain
    present in `desired`), so mailboxes in unrelated domains an operator
    added by hand are never removed."""
    managed_domains = {e.split("@", 1)[1] for e in desired if "@" in e}
    to_add = sorted(desired - current)
    to_del = sorted(
        e for e in (current - desired)
        if "@" in e and e.split("@", 1)[1] in managed_domains
        and e.split("@", 1)[0].lower() not in PROTECTED_LOCALPARTS
    )
    return to_add, to_del


# ─── Keycloak group resolution ─────────────────────────────────────────────


def _groups_api(env: dict) -> str:
    # KEYCLOAK_CLIENTS_API = .../admin/realms/<realm>/clients -> swap to groups.
    return env["KEYCLOAK_CLIENTS_API"].rsplit("/clients", 1)[0] + "/groups"


def managed_group_ids(env: dict, http_json, auth_hdr) -> list[str]:
    """IDs for /staff, /client, and /staff's department subgroups."""
    groups_api = _groups_api(env)
    tree = http_json(f"{groups_api}?briefRepresentation=false", auth_hdr, method="GET")
    ids: list[str] = []
    for g in tree or []:
        if g.get("name") in MANAGED_GROUPS:
            ids.append(g["id"])
            if g.get("name") == "staff":
                ids.extend(sub["id"] for sub in (g.get("subGroups") or []))
    return ids


def desired_emails(env: dict, http_json, auth_hdr) -> set[str]:
    groups_api = _groups_api(env)
    emails: set[str] = set()
    for gid in managed_group_ids(env, http_json, auth_hdr):
        members = http_json(f"{groups_api}/{gid}/members?max=-1", auth_hdr, method="GET")
        emails |= collect_member_emails(members)
    return emails


# ─── Orchestrator ──────────────────────────────────────────────────────────


def reconcile(env: dict, http_json, run=_default_run) -> None:
    """Reconcile DMS mailboxes to Keycloak membership. http_json matches
    dashboard-sync.http_json(url, headers, body=None, method=...). Non-fatal:
    raises nothing the caller must handle beyond the broad guard in main."""
    container = dms_container(run)
    if not container:
        return  # template not deployed
    secret = (env.get("DASHBOARD_SYNC_CLIENT_SECRET") or "").strip()
    if not secret:
        print("mailbox-sync: no service-account secret; skipping.")
        return

    import urllib.parse
    tok = http_json(
        env["KEYCLOAK_TOKEN_URL"],
        {"content-type": "application/x-www-form-urlencoded"},
        body=urllib.parse.urlencode({
            "grant_type": "client_credentials",
            "client_id": env["DASHBOARD_SYNC_CLIENT_ID"],
            "client_secret": secret,
        }),
        method="POST",
    )
    auth_hdr = {"authorization": f"Bearer {tok['access_token']}", "accept": "application/json"}

    desired = desired_emails(env, http_json, auth_hdr)
    if not desired:
        print("mailbox-sync: no staff/client members with email; skipping (refusing "
              "to treat an empty desired set as 'delete everything').")
        return

    rc, listing = run(["docker", "exec", container, "setup", "email", "list"])
    current = parse_account_list(listing) if rc == 0 else set()

    to_add, to_del = compute_actions(desired, current)
    for addr in to_add:
        # Random throwaway password: never used (OAuth2 is the auth path),
        # never stored by us; only satisfies DMS account-existence.
        run(["docker", "exec", container, "setup", "email", "add", addr,
             secrets.token_urlsafe(24)])
    for addr in to_del:
        run(["docker", "exec", container, "setup", "email", "del", "-y", addr])
    if to_add or to_del:
        print(f"mailbox-sync: +{len(to_add)} -{len(to_del)} mailbox(es).")
