"""What each page shows, and what it proves before it advances.

EVERY STEP VALIDATES BEFORE IT ADVANCES. An installer that collects six pages
of answers and discovers on the last one that the first credential was wrong
has spent a client's whole sitting to tell them something it knew at the start.

AND IT NEVER REPORTS SUCCESS FOR A HOST THAT DOES NOT WORK. That is the harder
half and it decides the shape of the probes below: a step passes only when the
thing it is about has been observed working, and a step that cannot observe
says so rather than assuming. A domain waiting on its registrar's nameservers
is the case that makes this concrete -- everything about it looks correct and
nothing it publishes resolves.

The FIELDS are the registry's; only the PROBES are here. That split is what
keeps the launcher free of knob knowledge: adding a question is a registry
edit, and adding a proof is a function here.
"""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import registry

CLOUDFLARE_API = "https://api.cloudflare.com/client/v4"
TAILSCALE_API = "https://api.tailscale.com/api/v2"


@dataclass
class Check:
    """One thing a step proved, or could not.

    `blocking` separates "this is wrong" from "this could not be established
    from here". An absence of evidence is not evidence of a fault, and a step
    that treated the two alike would either refuse valid installs or pass
    broken ones -- there is no setting of one flag that avoids both.
    """

    label: str
    ok: bool
    detail: str = ""
    blocking: bool = True

    @property
    def blocks(self) -> bool:
        return not self.ok and self.blocking


@dataclass
class Field:
    """One question on a page, resolved from the registry."""

    key: str
    secret: bool
    optional: bool
    options: list[str]
    default: str
    doc: str
    governor: str
    governor_values: list[str]

    def shown_for(self, answers: dict[str, str]) -> bool:
        """Whether this field applies, given what is answered so far.

        A field its governing choice does not use is not asked. Offering a
        Headscale server address to a client who chose the hosted network asks
        for a value nothing will ever read, and offering an OAuth pair to one
        who chose no private network at all asks for a credential to a network
        their server does not join.
        """
        if not self.governor:
            return True
        return answers.get(self.governor, "") in self.governor_values


@dataclass
class Step:
    name: str
    title: str
    doc: str
    validates: str
    fields: list[Field]


def _field(doc: dict, entry: dict) -> Field:
    governor, values = registry.governed_by(entry)
    panel = entry.get("panel") or {}
    return Field(
        key=entry["key"],
        secret=registry.is_secret(doc, entry["key"]),
        # A knob with no panel declaration has no optionality declared either.
        # Optional is the safe reading: a launcher that made an undeclared
        # field mandatory would block an install on a value nothing needs.
        optional=bool(panel.get("optional", True)),
        options=registry.options_for(entry),
        default=registry.default_for(entry),
        doc=str(entry.get("doc") or ""),
        governor=governor,
        governor_values=values,
    )


def build(doc: dict) -> list[Step]:
    """The whole wizard, from the registry."""
    return [
        Step(
            name=step["name"],
            title=step["title"],
            doc=step["doc"],
            validates=step["validates"],
            fields=[_field(doc, entry)
                    for entry in registry.step_fields(doc, step["name"])],
        )
        for step in registry.steps(doc)
    ]


# --- the probes --------------------------------------------------------------


def _http_json(url: str, *, headers: dict | None = None, data: bytes | None = None,
               timeout: float = 10.0) -> tuple[int, dict]:
    req = urllib.request.Request(url, headers=headers or {}, data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(body or "{}")
        except ValueError:
            return e.code, {"error": body[:200]}
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return 0, {"error": str(e)}


def _tcp_open(host: str, port: int, timeout: float = 5.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def check_target(answers: dict[str, str]) -> list[Check]:
    """The server answers, and the key that reaches it is on this machine."""
    host = (answers.get("HOST_PUBLIC_IP") or "").strip()
    port = (answers.get("HOST_SSH_PORT") or "22").strip() or "22"
    checks = [Check("an address was given", bool(host),
                    host or "no address, so there is nothing to install onto")]
    if host:
        checks.append(Check(
            f"{host}:{port} answers", _tcp_open(host, int(port)),
            "nothing accepted a connection. A server still being delivered is "
            "the ordinary reason, and this page is where a run waits for it"))
    for label, key in (("private key", "SSH_PRIVATE_KEY"),
                       ("public key", "SSH_PUBLIC_KEY_FILE")):
        raw = (answers.get(key) or "").strip()
        path = Path(os.path.expanduser(raw)) if raw else None
        # NOT blocking. A missing keypair is something the installer offers to
        # generate, so refusing here would block an install on a file that is
        # one prompt away.
        checks.append(Check(
            f"SSH {label}", bool(path and path.is_file()),
            f"{raw} is not on this machine; the installer offers to generate "
            "the pair" if raw else "no path given",
            blocking=False))
    return checks


def check_domain(answers: dict[str, str], secrets: dict[str, str]) -> list[Check]:
    """The token is live, it reaches this domain, and the domain is ACTIVE.

    The third is the one that costs an install. A zone sits at `pending` until
    the registrar's nameservers point at the pair Cloudflare assigned; records
    created on it through the API resolve NOWHERE, so an install would issue no
    certificate, publish a wildcard nobody can follow, and then wait forever on
    an address that never answers.
    """
    zone = (answers.get("CLOUDFLARE_ZONE") or "").strip()
    token = (secrets.get("cloudflare_api_token") or "").strip()
    if not zone and not token:
        return [Check("Cloudflare", True,
                      "no domain yet -- this server installs with no public "
                      "surface and publishes one from the panel later")]
    if not token:
        return [Check("Cloudflare token", False,
                      f"a domain was given ({zone}) and no token. The server "
                      "cannot create a single name under it")]
    if not zone:
        return [Check("Cloudflare domain", True,
                      "a token was given and no domain. It is stored, and the "
                      "public surface waits for a domain", blocking=False)]

    headers = {"Authorization": f"Bearer {token}"}
    status, body = _http_json(f"{CLOUDFLARE_API}/user/tokens/verify", headers=headers)
    live = status == 200 and (body.get("result") or {}).get("status") == "active"
    if not live:
        return [Check("the token is live", False,
                      f"HTTP {status} {(body.get('result') or {}).get('status', '')}".strip())]

    status, body = _http_json(f"{CLOUDFLARE_API}/zones?name={zone}", headers=headers)
    results = (body.get("result") or []) if status == 200 else []
    out = [Check("the token is live", True)]
    if not results:
        out.append(Check(f"the token reaches {zone}", False,
                         "the token is valid and grants nothing on this "
                         "domain. Check its Zone Resources"))
        return out
    out.append(Check(f"the token reaches {zone}", True))

    found = results[0]
    zone_status = str(found.get("status") or "unknown")
    if zone_status == "active":
        out.append(Check(f"{zone} is active", True, f"zone {found.get('id', '')}"))
        return out
    nameservers = ", ".join(found.get("name_servers") or []) or "the pair Cloudflare assigned"
    out.append(Check(
        f"{zone} is active", False,
        f"it is {zone_status}. Cloudflare serves no DNS for this domain until "
        f"the registrar delegates it to {nameservers}. This page is where a "
        "run waits for that"))
    return out


def check_access(answers: dict[str, str], secrets: dict[str, str]) -> list[Check]:
    """The credential works, and this machine can reach what it joins.

    Both, because a key minted for a network this machine cannot reach produces
    a server nobody here can administer -- and the install closes the public
    port on the strength of that network working.
    """
    method = (answers.get("ACCESS_METHOD") or "tailnet").strip()
    if method != "tailnet":
        return [Check("direct SSH on the public address", True,
                      "this server joins no private network and keeps its SSH "
                      "port open. The installer will not close it")]

    provider = (answers.get("TAILNET_PROVIDER") or "tailscale").strip() or "tailscale"
    if provider == "headscale":
        control = (answers.get("TAILNET_CONTROL_URL") or "").strip()
        has_key = bool((secrets.get("headscale_api_key") or "").strip()
                       or (secrets.get("headscale_preauth_key") or "").strip())
        out = [Check("a control server address", bool(control),
                     control or "Headscale is chosen and no address was given"),
               Check("a join credential", has_key,
                     "neither an API key nor a pre-authentication key was "
                     "given, and the server cannot join without one")]
        if control:
            status, _ = _http_json(f"{control.rstrip('/')}/health", timeout=8.0)
            out.append(Check(f"{control} answers", status != 0,
                             "the control server did not answer from this "
                             "machine", blocking=False))
        return out

    client_id = (secrets.get("tailscale_oauth_client_id") or "").strip()
    client_secret = (secrets.get("tailscale_oauth_client_secret") or "").strip()
    if not (client_id and client_secret):
        return [Check("the OAuth client", False,
                      "both the client id and the secret are needed to mint "
                      "the key that joins this server")]
    from base64 import b64encode

    auth = b64encode(f"{client_id}:{client_secret}".encode()).decode()
    status, body = _http_json(
        f"{TAILSCALE_API}/oauth/token",
        headers={"Authorization": f"Basic {auth}",
                 "Content-Type": "application/x-www-form-urlencoded"},
        data=b"grant_type=client_credentials")
    return [Check("the OAuth client exchanges for a token", status == 200,
                  f"HTTP {status} {(body or {}).get('error', '')}".strip())]


def check_backup(answers: dict[str, str]) -> list[Check]:
    """The endpoint answers.

    Whether the credentials open the repository is proven by the first backup,
    on the server, where restic runs. Checking it from here would need a second
    implementation of restic's own authentication, and a second implementation
    that disagreed with the first would be worse than no check.
    """
    repo = (answers.get("BACKUP_RESTIC_REPO") or "").strip()
    if not repo:
        return [Check("backup repository", True,
                      "none given. Backups are configured in the panel "
                      "afterwards, and nothing is scheduled until they are",
                      blocking=False)]
    endpoint = repo.split(":", 1)[1] if ":" in repo else repo
    endpoint = endpoint.split("/")[0] if "://" not in endpoint else endpoint
    host = endpoint.replace("https://", "").replace("http://", "").split("/")[0]
    host, _, port = host.partition(":")
    return [Check(f"{host} answers", _tcp_open(host, int(port or 443)),
                  "the backup endpoint did not accept a connection from this "
                  "machine")]


def check_locale(answers: dict[str, str]) -> list[Check]:
    """Nothing to prove from here: both are settings on the server itself.

    Stated as a passing check rather than as no check at all, so a client
    reading the page sees that the step was considered rather than skipped.
    """
    return [Check("language and timezone", True,
                  f"{answers.get('COMMON_LOCALE', '')} / "
                  f"{answers.get('COMMON_TIMEZONE', '')}")]


def check_keyset(answers: dict[str, str]) -> list[Check]:
    """The acknowledgement, and it is the one check that cannot be waived.

    `catena install` ends by showing three passwords once: the first-login
    password, the backup encryption password and the console break-glass
    password. Nothing off the server holds a copy. Without an explicit
    acknowledgement here the launcher ships installs nobody can recover.
    """
    acked = (answers.get("_keyset_acknowledged") or "").strip() == "yes"
    return [Check("the recovery passwords are saved", acked,
                  "the installer shows them once and nothing else holds a "
                  "copy, so this cannot be skipped")]


# The probe for each step, by name. A step with no entry has nothing to prove
# from here -- and there are none today, which is the point of listing them all
# rather than defaulting.
PROBES = {
    "target": lambda a, s: check_target(a),
    "domain": check_domain,
    "access": check_access,
    "backup": lambda a, s: check_backup(a),
    "locale": lambda a, s: check_locale(a),
    "keyset": lambda a, s: check_keyset(a),
}


def validate(step: str, answers: dict[str, str], secrets: dict[str, str]) -> list[Check]:
    probe = PROBES.get(step)
    if probe is None:
        return []
    return probe(answers, secrets)


def blocked(checks: list[Check]) -> bool:
    return any(check.blocks for check in checks)
