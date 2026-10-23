"""The checks only a machine other than the server can make, run from the
installer's machine once the server's own checks have passed.

  - its public IP answers on no TCP port the public-port registry does not
    declare (a scan of every port, recorded on the server for the panel's
    port-exposure card);
  - the containers' network is not reachable from here: a container IP that
    answers would bypass Traefik and the apps' sign-in.

That the installer reached the server over SSH at all is the access check.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import shlex
import socket
import time
from typing import Callable

from .remote import Session

SUMMARY = "/etc/catena/public-ports.summary.json"
RECORD = "/var/lib/catena/port-scan.json"
NETWORK = "catena-network"


def _private(address: str) -> bool:
    """RFC 1918, CGNAT (also the tailnet's), loopback, link-local: an address
    a scan from a machine on the same network would find open for reasons
    that say nothing about the internet."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return True
    cgnat = ipaddress.ip_network("100.64.0.0/10")
    return (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_unspecified or (ip.version == 4 and ip in cgnat))


async def _answers(host: str, port: int, timeout: float) -> bool:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except (asyncio.TimeoutError, OSError):
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return True


async def _scan(host: str, ports: range, concurrency: int, timeout: float) -> list[int]:
    # `concurrency` workers draw from one iterator, so that many probes are in
    # flight at a time.
    pending = iter(ports)
    found: list[int] = []

    async def worker() -> None:
        for port in pending:
            if await _answers(host, port, timeout):
                found.append(port)

    await asyncio.gather(*(worker() for _ in range(concurrency)))
    return sorted(found)


def scan(host: str, ports: range = range(1, 65536), *, concurrency: int = 500,
         timeout: float = 1.0) -> list[int]:
    """The ports on `host` that complete a TCP handshake from here."""
    return asyncio.run(_scan(host, ports, concurrency, timeout))


def _root(session: Session) -> str:
    return "" if session.user == "root" else "sudo -n "


def public_ports(session: Session, public_ip: str, line: Callable[[str], None]) -> bool:
    if not public_ip or _private(public_ip):
        line(f"catena-installer: public port scan SKIPPED -- {public_ip or 'no public IP'} "
             "is not a routable address, so a scan from here says nothing about "
             "what the internet reaches. Set HOST_PUBLIC_IP to the server's "
             "public IPv4 to enable it.")
        return True
    rc, raw = session.output(f"{_root(session)}cat {SUMMARY}")
    try:
        expected = sorted(int(p) for p in json.loads(raw)["public_open_tcp"]) if rc == 0 else []
    except (ValueError, KeyError, TypeError):
        expected = []
    line(f"catena-installer: scanning every TCP port of {public_ip} from this machine")
    open_ports = scan(public_ip)
    unexpected = sorted(set(open_ports) - set(expected))
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "host": public_ip,
        "scanned": 65535,
        "open_ports": open_ports,
        "expected": expected,
        "expected_count": len(expected),
        "unexpected": unexpected,
        "exposed_count": len(unexpected),
    }
    body = json.dumps(record, indent=2)
    session.output(f"printf '%s\\n' {shlex.quote(body)} | {_root(session)}tee {RECORD} >/dev/null")
    if unexpected:
        line(f"catena-installer: SECURITY REGRESSION: {public_ip} answers on {unexpected}, "
             f"which nothing declares (expected open: {expected}). Check ufw, "
             "docker's iptables rules and the provider's firewall.")
        return False
    line(f"catena-installer: {public_ip} is sealed: 65535 ports scanned, "
         f"{len(open_ports)} open, all declared")
    return True


def container_network(session: Session, line: Callable[[str], None]) -> bool:
    template = "{{range $k, $v := .Containers}}{{$v.IPv4Address}}{{\"\\n\"}}{{end}}"
    rc, raw = session.output(
        f"{_root(session)}docker network inspect {NETWORK} --format {shlex.quote(template)}")
    if rc != 0:
        line(f"catena-installer: cannot list the containers on {NETWORK}: {raw.strip()}")
        return False
    addresses = sorted({a.split("/")[0] for a in raw.split() if a.strip()})
    reached = []
    for address in addresses:
        try:
            with socket.create_connection((address, 80), timeout=3):
                reached.append(address)
        except OSError:
            pass
    if reached:
        line(f"catena-installer: container IPs {reached} on {NETWORK} answer from this "
             "machine: the network the apps sit on is reachable around Traefik.")
        return False
    line(f"catena-installer: {len(addresses)} container IP(s) on {NETWORK}, none "
         "reachable from this machine")
    return True


def run(session: Session, public_ip: str, line: Callable[[str], None]) -> bool:
    ok = container_network(session, line)
    return public_ports(session, public_ip, line) and ok
