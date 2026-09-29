"""What each section asks, and what it proves before the install starts.

A SECTION WITH A CHECK PROVES SOMETHING REAL, and Install runs every check
first. An installer that collects every answer and discovers at the end that
the first credential was wrong has spent a client's whole sitting to tell them
something it knew at the start. A section with nothing to observe -- answers
picked from a list -- has no check at all, rather than one that passes on
anything.

AND IT NEVER REPORTS SUCCESS FOR A HOST THAT DOES NOT WORK. A step passes only
when the thing it is about has been observed working, and a step that cannot
observe says so rather than assuming. A domain waiting on its registrar's
nameservers is the case that makes this concrete -- everything about it looks
correct and nothing it publishes resolves.

OPTIONAL SECTIONS ARE ALL OR NOTHING. The domain and its token, the tailnet and
its credential, the backup repository and its keys: blank is a supported
answer, and half of one is refused, because it describes nothing the server
can use.

The FIELDS are the registry's; only the PROBES, and the few answers that follow
from other answers, are here. That split is what keeps the launcher free of
knob knowledge: adding a question is a registry edit, and adding a proof is a
function here.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import json
import os
import socket
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field as _dc_field
from pathlib import Path

from . import registry

CLOUDFLARE_API = "https://api.cloudflare.com/client/v4"
TAILSCALE_API = "https://api.tailscale.com/api/v2"

# The host-only UIs and the account that forwards to them. Product constants
# (catena-ce playbooks/group_vars/all/main.yml), printed on the access page.
PANEL_PORT = 9010
PORTAINER_PORT = 9000
PANEL_USER = "panel"


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
    """One question in a section, resolved from the registry."""

    key: str
    secret: bool
    optional: bool
    options: list[str]
    default: str
    doc: str
    governor: str
    governor_values: list[str]
    example: str = ""

    def shown_for(self, answers: dict[str, str]) -> bool:
        """Whether this field applies, given what is answered so far.

        A field its governing choice does not use is not asked. Offering a
        Headscale server address to a client who chose the hosted network asks
        for a value nothing will ever read.
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
    links: list[dict] = _dc_field(default_factory=list)


def _field(doc: dict, entry: dict, on_page: set[str]) -> Field:
    governor, values = registry.governed_by(entry)
    # A governor the launcher does not ask for cannot be evaluated here, so the
    # field is shown: the panel's `depends` still applies on the panel.
    if governor not in on_page:
        governor, values = "", []
    return Field(
        key=entry["key"],
        secret=registry.is_secret(doc, entry["key"]),
        optional=not registry.is_required(entry),
        options=registry.options_for(entry),
        default=registry.default_for(entry),
        doc=str(entry.get("doc") or ""),
        governor=governor,
        governor_values=values,
        example=registry.example_for(entry),
    )


def build(doc: dict) -> list[Step]:
    """The whole wizard, from the registry."""
    on_page = {entry["key"] for step in registry.steps(doc)
               for entry in registry.step_fields(doc, step["name"])}
    return [
        Step(
            name=step["name"],
            title=step["title"],
            doc=step["doc"],
            validates=str(step.get("validates") or ""),
            fields=[_field(doc, entry, on_page)
                    for entry in registry.step_fields(doc, step["name"])],
            links=list(step.get("links") or []),
        )
        for step in registry.steps(doc)
    ]


def missing_required(step: Step, values: dict[str, str]) -> list[Check]:
    """A blocking line for each required field left empty. Shown before the
    step's own probes, which have nothing to observe without it."""
    return [Check(f"{f.key} is required", False, "fill it in")
            for f in step.fields
            if not f.optional and f.shown_for(values)
            and not (values.get(f.key) or "").strip()]


# --- answers that follow from other answers ---------------------------------


def _tailnet_credentials_given(answers: dict[str, str], secrets: dict[str, str]) -> bool:
    if (answers.get("TAILNET_PROVIDER") or "").strip() == "headscale":
        return bool((answers.get("TAILNET_CONTROL_URL") or "").strip()
                    and ((secrets.get("headscale_api_key") or "").strip()
                         or (secrets.get("headscale_preauth_key") or "").strip()))
    return bool((secrets.get("tailscale_oauth_client_id") or "").strip()
                and (secrets.get("tailscale_oauth_client_secret") or "").strip())


def derive(answers: dict[str, str], secrets: dict[str, str]) -> dict[str, str]:
    """Answers the page does not ask for because the others decide them.

    The access method is whether the server joins a tailnet, which is whether
    the tailnet section was answered. Asking for it separately let a client
    choose `tailnet` with no credential, or `public_ssh` beside one.
    """
    return {"ACCESS_METHOD": ("tailnet" if _tailnet_credentials_given(answers, secrets)
                              else "public_ssh")}


# --- the probes --------------------------------------------------------------


def _http_json(url: str, *, headers: dict | None = None, data: bytes | None = None,
               timeout: float = 10.0, method: str | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(url, headers=headers or {}, data=data, method=method)
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


def _ssh_banner(host: str, port: int, timeout: float = 6.0) -> str:
    """The first line the port sends, or "" when nothing answered. An SSH
    server speaks first, with `SSH-2.0-...`; anything else on the port is not
    one."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            return sock.recv(256).decode("ascii", "replace").strip()
    except OSError:
        return ""


def _ssh_login(host: str, port: int, user: str, key: str) -> tuple[bool, str]:
    """Whether `user` logs in with `key` alone. BatchMode: no password prompt,
    no host-key prompt. The host key is not recorded -- this is a probe, and a
    probe that edits the client's known_hosts leaves something behind."""
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
            "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR", "-i", key, "-p", str(port), f"{user}@{host}", "true"]
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=25)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    return out.returncode == 0, (out.stderr or "").strip()[-200:]


def check_target(answers: dict[str, str]) -> list[Check]:
    """An SSH server answers, and it lets this key in.

    Both, because the graphical installer has no password path: the server
    has to accept the key for its initial login already, or the first leg of
    the install stops. On a server a previous run already hardened, root is
    refused and the ops account takes the same key, so either one passes.
    """
    host = (answers.get("HOST_PUBLIC_IP") or "").strip()
    port_raw = (answers.get("HOST_SSH_PORT") or "22").strip() or "22"
    if not host or not port_raw.isdigit():
        return [] if not host else [Check("the SSH port", False,
                                          f"{port_raw!r} is not a port number")]
    port = int(port_raw)
    banner = _ssh_banner(host, port)
    checks = [Check(f"an SSH server answers at {host}:{port}",
                    banner.startswith("SSH-"),
                    banner or "nothing accepted a connection. A server still "
                    "being delivered is the ordinary reason, and this section is "
                    "where a run waits for it")]
    raw = (answers.get("SSH_PRIVATE_KEY") or "").strip()
    key = os.path.expanduser(raw) if raw else ""
    pub = os.path.expanduser((answers.get("SSH_PUBLIC_KEY_FILE") or "").strip())
    have_key = bool(key) and Path(key).is_file() and bool(pub) and Path(pub).is_file()
    checks.append(Check("the keypair is on this machine", have_key,
                        "" if have_key else f"{raw or '(no path)'} or its .pub is "
                        "missing; generate one with ssh-keygen -t ed25519"))
    if not (checks[0].ok and have_key):
        return checks
    tried = []
    for user in dict.fromkeys(((answers.get("HOST_INITIAL_USER") or "root").strip(),
                               (answers.get("OPS_USER") or "ops").strip())):
        ok, why = _ssh_login(host, port, user, key)
        if ok:
            checks.append(Check(f"{user} logs in with this key", True))
            return checks
        tried.append(f"{user}: {why or 'refused'}")
    checks.append(Check("the server accepts this key", False,
                        "; ".join(tried) + ". Add the public key to the "
                        "server's initial login (most providers take it when "
                        "the server is ordered)"))
    return checks


def zone_choices(secrets: dict[str, str]) -> list[str]:
    """The domains the token reaches, for the domain list. Empty when there is
    no token or it reaches nothing."""
    token = (secrets.get("cloudflare_api_token") or "").strip()
    if not token:
        return []
    status, body = _http_json(f"{CLOUDFLARE_API}/zones?per_page=50",
                              headers={"Authorization": f"Bearer {token}"})
    if status != 200:
        return []
    return sorted(str(z.get("name")) for z in body.get("result") or [] if z.get("name"))


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
                      "none -- this server installs with no public surface, and "
                      "both can be entered in the panel afterwards")]
    if not token:
        return [Check("the Cloudflare token", False,
                      f"a domain was given ({zone}) and no token to publish it")]

    headers = {"Authorization": f"Bearer {token}"}
    status, body = _http_json(f"{CLOUDFLARE_API}/user/tokens/verify", headers=headers)
    live = status == 200 and (body.get("result") or {}).get("status") == "active"
    if not live:
        return [Check("the token is live", False,
                      f"HTTP {status} {(body.get('result') or {}).get('status', '')}".strip())]
    out = [Check("the token is live", True)]
    if not zone:
        out.append(Check("a domain is chosen", False,
                         "pick the domain this token publishes from the list"))
        return out

    status, body = _http_json(f"{CLOUDFLARE_API}/zones?name={urllib.parse.quote(zone)}",
                              headers=headers)
    results = (body.get("result") or []) if status == 200 else []
    if not results:
        out.append(Check(f"the token reaches {zone}", False,
                         "the token is valid and grants nothing on this domain. "
                         "Check its Zone Resources"))
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
        f"the registrar delegates it to {nameservers}. This section is where a "
        "run waits for that"))
    return out


def check_access(answers: dict[str, str], secrets: dict[str, str]) -> list[Check]:
    """The credential works at the provider, and this machine reaches what the
    server joins. Blank is a supported answer: SSH stays the way in."""
    provider = (answers.get("TAILNET_PROVIDER") or "tailscale").strip() or "tailscale"
    if provider == "headscale":
        return _check_headscale(answers, secrets)

    client_id = (secrets.get("tailscale_oauth_client_id") or "").strip()
    client_secret = (secrets.get("tailscale_oauth_client_secret") or "").strip()
    if not client_id and not client_secret:
        return [Check("private network", True,
                      "none -- SSH on the public address stays the way in, "
                      "and a tailnet can be added in the panel afterwards")]
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
    out = [Check("the OAuth client exchanges for a token", status == 200,
                 f"HTTP {status} {(body or {}).get('error', '')}".strip())]
    token = str((body or {}).get("access_token") or "")
    if status == 200 and token:
        # The lockdown asks the control server whether this server is connected
        # before it closes public SSH, and that needs the device read scope.
        dstatus, _ = _http_json(f"{TAILSCALE_API}/tailnet/-/devices",
                                headers={"Authorization": f"Bearer {token}"})
        out.append(Check("the OAuth client can read devices", dstatus == 200,
                         f"HTTP {dstatus}: add Devices > Core (read) to the "
                         f"client's scopes"))
    return out


def _check_headscale(answers: dict[str, str], secrets: dict[str, str]) -> list[Check]:
    control = (answers.get("TAILNET_CONTROL_URL") or "").strip().rstrip("/")
    api_key = (secrets.get("headscale_api_key") or "").strip()
    preauth = (secrets.get("headscale_preauth_key") or "").strip()
    if not control and not api_key and not preauth:
        return [Check("private network", True,
                      "none -- SSH on the public address stays the way in, "
                      "and a tailnet can be added in the panel afterwards")]
    if not control:
        return [Check("a control server address", False,
                      "Headscale is chosen and no address was given")]
    if not (api_key or preauth):
        return [Check("a join credential", False,
                      "neither an API key nor a pre-authentication key was "
                      "given, and the server cannot join without one")]
    if api_key:
        # The same call the lockdown makes to ask whether the node is online.
        status, body = _http_json(f"{control}/api/v1/node",
                                  headers={"Authorization": f"Bearer {api_key}"},
                                  timeout=8.0)
        return [Check(f"{control} accepts the API key", status == 200,
                      f"HTTP {status} {(body or {}).get('error', '')}".strip())]
    status, _ = _http_json(f"{control}/health", timeout=8.0)
    return [
        Check(f"{control} answers", status != 0,
              "the control server did not answer from this machine"),
        Check("the pre-authentication key", True,
              "a pre-authentication key cannot be tested without using it up. "
              "With no API key, a lockdown applied later from the panel cannot "
              "ask Headscale whether this server is online, and refuses",
              blocking=False),
    ]


def _s3_endpoint(repo: str) -> tuple[str, str, str]:
    """(scheme, host, bucket) from a restic S3 repository address:
    `s3:https://host/bucket[/prefix]` or `s3:host/bucket[/prefix]`."""
    rest = repo[3:] if repo.startswith("s3:") else repo
    scheme = "https"
    if "://" in rest:
        scheme, rest = rest.split("://", 1)
    host, _, path = rest.partition("/")
    return scheme, host, path.split("/", 1)[0]


def _sigv4_head_bucket(scheme: str, host: str, bucket: str, access: str,
                       secret: str, region: str) -> tuple[int, dict[str, str]]:
    """HEAD /<bucket>, signed with AWS Signature Version 4 -- the request every
    S3-compatible store authenticates the same way restic's own do."""
    now = _dt.datetime.now(_dt.timezone.utc)
    amz_date, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    payload = hashlib.sha256(b"").hexdigest()
    canonical_uri = "/" + urllib.parse.quote(bucket)
    signed = "host;x-amz-content-sha256;x-amz-date"
    canonical = "\n".join(["HEAD", canonical_uri, "",
                           f"host:{host}\nx-amz-content-sha256:{payload}\n"
                           f"x-amz-date:{amz_date}\n", signed, payload])
    scope = f"{day}/{region}/s3/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope,
                         hashlib.sha256(canonical.encode()).hexdigest()])
    k = f"AWS4{secret}".encode()
    for part in (day, region, "s3", "aws4_request"):
        k = hmac.new(k, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    headers = {
        "x-amz-date": amz_date, "x-amz-content-sha256": payload,
        "Authorization": (f"AWS4-HMAC-SHA256 Credential={access}/{scope}, "
                          f"SignedHeaders={signed}, Signature={signature}"),
    }
    req = urllib.request.Request(f"{scheme}://{host}{canonical_uri}",
                                 headers=headers, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310
            return r.status, dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers)
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0, {}


def check_backup(answers: dict[str, str], secrets: dict[str, str]) -> list[Check]:
    """The keys open the bucket: a signed request is accepted by the endpoint.

    The region is the store's to state. us-east-1 is asked first, which every
    S3-compatible store that has no regions accepts; a store that has them
    names the right one in its refusal, and the request is signed again for it.
    """
    repo = (answers.get("BACKUP_RESTIC_REPO") or "").strip()
    access = (secrets.get("backup_s3_access_key") or "").strip()
    secret = (secrets.get("backup_s3_secret_key") or "").strip()
    if not (repo or access or secret):
        return [Check("backup", True,
                      "none -- nothing is backed up until the repository and "
                      "its keys are entered in the panel")]
    if not (repo and access and secret):
        return [Check("the repository and both keys", False,
                      "a backup needs all three, or none of them")]
    scheme, host, bucket = _s3_endpoint(repo)
    if not (host and bucket):
        return [Check("the repository address", False,
                      "expected s3:https://<endpoint>/<bucket>")]
    status, headers = _sigv4_head_bucket(scheme, host, bucket, access, secret,
                                         "us-east-1")
    region = headers.get("x-amz-bucket-region") or headers.get("X-Amz-Bucket-Region")
    if status in (301, 400) and region and region != "us-east-1":
        status, headers = _sigv4_head_bucket(scheme, host, bucket, access, secret,
                                             region)
    why = {0: f"{host} did not answer from this machine",
           403: "the endpoint refused these keys for this bucket",
           404: f"there is no bucket {bucket} at {host}"}
    return [Check(f"the keys open {bucket} at {host}", status == 200,
                  "" if status == 200 else why.get(status, f"HTTP {status}"))]


def check_keyset(answers: dict[str, str]) -> list[Check]:
    """The acknowledgement, and it is the one check that cannot be waived.

    `catena-cli install` ends by showing three passwords once: the first-login
    password, the backup encryption password and the console break-glass
    password, with the journal verification key. Nothing off the server holds
    a copy. Without an explicit
    acknowledgement here the launcher ships installs nobody can recover.
    """
    acked = (answers.get("_keyset_acknowledged") or "").strip() == "yes"
    return [Check("the recovery passwords are saved", acked,
                  "the installer shows them once and nothing else holds a "
                  "copy, so this cannot be skipped")]


# The probe for each step that has one, by name.
PROBES = {
    "target": lambda a, s: check_target(a),
    "domain": check_domain,
    "access": check_access,
    "backup": check_backup,
    "keyset": lambda a, s: check_keyset(a),
}

# Choice lists a check fills in: after the domain step is checked, the domain
# field offers the zones the token reaches.
CHECK_OPTIONS = {
    "domain": {"CLOUDFLARE_ZONE": lambda a, s: zone_choices(s)},
}


def validate(step: str, answers: dict[str, str], secrets: dict[str, str]) -> list[Check]:
    probe = PROBES.get(step)
    if probe is None:
        return []
    return probe(answers, secrets)


def options_after_check(step: str, answers: dict[str, str],
                        secrets: dict[str, str]) -> dict[str, list[str]]:
    return {key: fill(answers, secrets)
            for key, fill in (CHECK_OPTIONS.get(step) or {}).items()}


def blocked(checks: list[Check]) -> bool:
    return any(check.blocks for check in checks)


# --- the access page --------------------------------------------------------


@dataclass
class WayIn:
    """One path to the panel: what it needs, and what to type."""

    title: str
    when: str
    commands: list[str]
    urls: list[str]
    login: str


def _hosts_address(inventory_dir: Path) -> str:
    """The address the installer's inventory reaches the server at: the
    tailnet address once the install's last leg joined one."""
    try:
        import yaml

        doc = yaml.safe_load((inventory_dir / "hosts.yml").read_text()) or {}
    except (OSError, ImportError, ValueError):
        return ""
    hosts = ((doc.get("all") or {}).get("children") or {}).get("vps", {}).get("hosts") or {}
    for entry in hosts.values():
        if isinstance(entry, dict) and entry.get("ansible_host"):
            return str(entry["ansible_host"])
    return ""


def ways_in(answers: dict[str, str], secrets: dict[str, str],
            inventory_dir: Path) -> list[WayIn]:
    """How the panel is reached after this install, for the access page.

    SSH always: the panel and Portainer answer the server's loopback, and a
    forward as the panel account -- which can do nothing but forward -- lands
    there. The tailnet and the Cloudflare tunnel are listed when this install
    configures them.
    """
    host = (answers.get("HOST_PUBLIC_IP") or "").strip() or "<server-address>"
    port = (answers.get("HOST_SSH_PORT") or "22").strip() or "22"
    key = (answers.get("SSH_PRIVATE_KEY") or "").strip()
    email = (answers.get("ADMIN_EMAIL") or "").strip() or "the admin email"
    login = (f"Panel: {email} and the admin password the install shows once. "
             "Portainer: admin and the same password.")

    def forward(address: str) -> str:
        opts = f" -i {key}" if key else ""
        opts += f" -p {port}" if port != "22" else ""
        return (f"ssh -N -L {PANEL_PORT}:127.0.0.1:{PANEL_PORT} "
                f"-L {PORTAINER_PORT}:127.0.0.1:{PORTAINER_PORT}{opts} "
                f"{PANEL_USER}@{address}")

    local = [f"http://localhost:{PANEL_PORT}", f"http://localhost:{PORTAINER_PORT}"]
    out = [WayIn(
        "SSH", "Always. Public SSH stays open until Lockdown is chosen in the "
        "panel, and reopens by itself while the tailnet is down.",
        [forward(host)], local, login)]

    if derive(answers, secrets)["ACCESS_METHOD"] == "tailnet":
        address = _hosts_address(inventory_dir)
        joined = address.startswith("100.")
        out.append(WayIn(
            "The tailnet", "From a device on the same tailnet. Required once "
            "Lockdown has closed public SSH.",
            [forward(address if joined else "<server-tailnet-address>")], local,
            login + ("" if joined else " The tailnet address is known once "
                     "the install has joined the server to the tailnet.")))

    zone = (answers.get("CLOUDFLARE_ZONE") or "").strip()
    if zone and (secrets.get("cloudflare_api_token") or "").strip():
        portainer = (answers.get("PORTAINER_SUBDOMAIN") or "portainer").strip()
        out.append(WayIn(
            "Cloudflare", "From anywhere, through the sign-on page.", [],
            [f"https://dash.{zone}", f"https://{portainer}.{zone}"],
            f"Sign on as {email} with the admin password. Portainer then asks "
            "for its own login: admin and the same password."))
    return out
