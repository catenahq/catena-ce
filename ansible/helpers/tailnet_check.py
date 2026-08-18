"""Controller-side tailnet preflight: is THIS machine on the tailnet?

Every stage after `bootstrap` reaches the VPS at its tailnet address --
bootstrap rewrites the inventory's `ansible_host` to the CGNAT IP before it
finishes -- so a controller that never joined the tailnet gets a full,
successful bootstrap and then an opaque SSH timeout to a 100.x address at the
first task of `site`. The VPS is by then baselined, hardened, keyed and
joined. Nothing before this check noticed, because the Tailscale OAuth probe
preflight already runs talks to api.tailscale.com over the plain internet and
passes fine from a machine that has never run `tailscale up`.

Two questions, strongest first:

  1. Is this machine a node of the tailnet the OAuth client belongs to?
     Answerable only when the credential can read the device list. Production
     clients are scoped `Auth Keys: Write` alone and get 403, so this degrades
     to UNKNOWN rather than failing -- a narrow scope is the documented
     production setup, not an error. (The test bench's client carries
     `Devices Core: Write` as well, so the bench takes the strong path.)
  2. Failing that: is Tailscale installed and actually up on this machine?
     That one is always answerable and is the pass/fail gate.

Stdlib only, and no import of anything on the target: this runs on the
operator's own machine, before any host exists.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field

API_BASE = "https://api.tailscale.com/api/v2"

# `tailscale status --json` BackendState when the node is up and authenticated.
BACKEND_RUNNING = "Running"

# membership() verdicts.
MEMBER = "member"
NOT_MEMBER = "not_member"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class LocalState:
    """What `tailscale status --json` says about this machine."""
    installed: bool = False
    backend_state: str = ""
    ipv4: str = ""
    magic_dns_suffix: str = ""
    detail: str = ""

    @property
    def running(self) -> bool:
        return self.backend_state == BACKEND_RUNNING and bool(self.ipv4)


@dataclass(frozen=True)
class Result:
    ok: bool
    lines: list[str] = field(default_factory=list)
    remedy: str = ""


def _first_ipv4(addresses) -> str:
    for a in addresses or []:
        if isinstance(a, str) and ":" not in a and a.strip():
            return a.strip()
    return ""


def local_state(*, timeout: int = 10) -> LocalState:
    """Read this machine's Tailscale state.

    `tailscale status --json` exits nonzero when the node is logged out but
    still prints usable JSON on stdout, so the return code is not the signal --
    the parsed BackendState is.
    """
    exe = shutil.which("tailscale")
    if not exe:
        return LocalState(detail="the tailscale CLI is not on PATH")
    try:
        proc = subprocess.run([exe, "status", "--json"], capture_output=True,
                              text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return LocalState(installed=True, detail=f"could not run `tailscale status`: {e}")
    try:
        status = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        return LocalState(installed=True,
                          detail=detail[0] if detail else "no JSON from `tailscale status`")
    self_node = status.get("Self") or {}
    suffix = status.get("MagicDNSSuffix") or ""
    if not suffix:
        suffix = (status.get("CurrentTailnet") or {}).get("MagicDNSSuffix") or ""
    return LocalState(
        installed=True,
        backend_state=str(status.get("BackendState") or ""),
        ipv4=_first_ipv4(self_node.get("TailscaleIPs")),
        magic_dns_suffix=suffix,
    )


def membership(ipv4: str, token: str, *, timeout: int = 15) -> tuple[str, str]:
    """Is `ipv4` one of the addresses in the token's tailnet?

    Returns (verdict, detail). Anything that is not a clean 200 answers
    UNKNOWN: a 403 means the OAuth client is scoped `Auth Keys: Write` only,
    which is the correct production scope, and a check that cannot see is not
    a check that failed.
    """
    if not ipv4 or not token:
        return UNKNOWN, "no tailnet IPv4 or no API token to ask with"
    req = urllib.request.Request(f"{API_BASE}/tailnet/-/devices")
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        if e.code == 403:
            return UNKNOWN, ("the OAuth client cannot read the device list "
                             "(scope is Auth Keys only) -- tailnet identity not compared")
        return UNKNOWN, f"device list unavailable (HTTP {e.code})"
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        return UNKNOWN, f"device list unavailable ({e})"
    addresses = set()
    for d in payload.get("devices") or []:
        for a in d.get("addresses") or []:
            if isinstance(a, str) and ":" not in a:
                addresses.add(a.strip())
    if not addresses:
        return UNKNOWN, "the device list came back with no addresses to compare"
    if ipv4 in addresses:
        return MEMBER, f"{ipv4} is a node of the tailnet these credentials belong to"
    return NOT_MEMBER, (
        f"{ipv4} is NOT in that tailnet's {len(addresses)} device address(es) -- "
        "this machine is joined to a DIFFERENT tailnet than the credentials"
    )


def install_instructions(control_url: str = "") -> str:
    """What to do about a failed check, on the machine that failed it."""
    join = ("sudo tailscale up" if not control_url
            else f"sudo tailscale up --login-server {control_url}")
    return "\n".join([
        "  Install Tailscale on THIS machine:",
        "    Linux           curl -fsSL https://tailscale.com/install.sh | sh",
        "    macOS/Windows   https://tailscale.com/download",
        "",
        "  Then join and confirm:",
        f"    {join}",
        "    tailscale status",
        "",
        "  The login is browser-based and cannot be automated. The VPS joins",
        "  the tailnet during bootstrap and every stage after it is reached at",
        "  a tailnet address, so this machine has to be on the same network",
        "  before the install starts.",
    ])


def check(*, token: str = "", control_url: str = "") -> Result:
    """The pass/fail gate. `token` is a Tailscale API bearer token when one is
    already in hand (seed exchanges the OAuth creds for one anyway); without it
    only the local state is checked. `control_url` non-empty means Headscale,
    where there is no Tailscale API to compare against."""
    state = local_state()
    if not state.installed:
        return Result(False, ["Tailscale CLI not installed on this machine"],
                      install_instructions(control_url))
    if not state.running:
        detail = state.detail or f"BackendState={state.backend_state or 'unknown'}"
        if state.backend_state == BACKEND_RUNNING and not state.ipv4:
            detail = "tailscaled is running but this machine has no tailnet IPv4"
        return Result(False, [f"Tailscale is installed but not up -- {detail}"],
                      install_instructions(control_url))

    where = f"this machine is on the tailnet as {state.ipv4}"
    if state.magic_dns_suffix:
        where += f" ({state.magic_dns_suffix})"
    lines = [where]
    if control_url:
        # Headscale: the tailnet is the operator's own server, and
        # api.tailscale.com knows nothing about it. Up is all there is to check.
        lines.append(f"control server {control_url} -- membership not comparable via the Tailscale API")
        return Result(True, lines)
    verdict, detail = membership(state.ipv4, token)
    lines.append(detail)
    if verdict == NOT_MEMBER:
        return Result(False, lines, install_instructions(control_url))
    return Result(True, lines)


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--control-url", default="",
                    help="Headscale base URL (TAILNET_CONTROL_URL); empty means "
                         "Tailscale SaaS")
    args = ap.parse_args(argv)
    # No --token: a bearer token on argv would land in `ps` and in the printed
    # command. Callers holding one (seed.py) use check() directly.
    result = check(control_url=args.control_url)
    for line in result.lines:
        print(f"  {'+' if result.ok else 'x'} {line}", file=sys.stderr)
    if not result.ok:
        print(f"\n{result.remedy}\n", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
