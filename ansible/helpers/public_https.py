#!/usr/bin/env python3
"""GET https://<name>/... the way the internet reaches this server: <name> is
resolved by public resolvers rather than by the machine making the request.

The checks that prove a server answers on its public domain run on the server
itself, or on the installer's machine. Through that machine's resolver they
answer a different question: what THIS network thinks the name is. A LAN DNS
filter that blocks the domain answers 0.0.0.0, the request lands on the machine
making it, and a working edge reads as a broken one.

So <name> is asked of 1.1.1.1, then 9.9.9.9. Only when neither answers at all
(a network that lets DNS out to its own resolver only) does the local resolver
decide, and the result names which one did. A public "no such name" is the
internet's answer, so it is final.

IPv4 only: a machine with no IPv6 route picks a AAAA answer it cannot reach,
and every name here has an A record.

Stdlib only, so it runs on a host's bare python and on an installer's machine.

    public_https.py --url https://auth.example.com/realms/vps/account

prints one JSON object: status (-1 when no HTTP answer came back), location,
body (the first 64 KiB), address, resolver, error.
"""
from __future__ import annotations

import argparse
import http.client
import json
import secrets
import socket
import ssl
import struct
import sys
import urllib.parse

PUBLIC_RESOLVERS = ("1.1.1.1", "9.9.9.9")
DNS_TIMEOUT_S = 3.0
BODY_LIMIT = 64 * 1024

_TYPE_A = 1
_CLASS_IN = 1
_RCODE_NXDOMAIN = 3


def _not_found(name: str, server: str) -> socket.gaierror:
    # The local resolver's own wording, so a caller that already tells "no
    # such name" apart from a refused connection keeps doing so.
    return socket.gaierror(
        socket.EAI_NONAME, f"Name or service not known ({name}, asked {server})")


def _skip_name(data: bytes, i: int) -> int:
    while True:
        length = data[i]
        if length == 0:
            return i + 1
        if length & 0xC0 == 0xC0:
            return i + 2
        i += length + 1


def parse_response(data: bytes, query_id: int, name: str, server: str) -> list[str] | None:
    """The A records in a DNS response. None for a response that cannot be
    used: another query's id, a truncated record, a server failure. Raises for
    NXDOMAIN."""
    try:
        rid, flags, qdcount, ancount, _, _ = struct.unpack(">HHHHHH", data[:12])
        if rid != query_id:
            return None
        rcode = flags & 0x000F
        if rcode == _RCODE_NXDOMAIN:
            raise _not_found(name, server)
        if rcode != 0:
            return None
        i = 12
        for _ in range(qdcount):
            i = _skip_name(data, i) + 4
        found = []
        for _ in range(ancount):
            i = _skip_name(data, i)
            rtype, rclass, _ttl, rdlength = struct.unpack(">HHIH", data[i:i + 10])
            i += 10
            if i + rdlength > len(data):
                return None
            if rtype == _TYPE_A and rclass == _CLASS_IN and rdlength == 4:
                found.append(socket.inet_ntoa(data[i:i + 4]))
            i += rdlength
        return found
    except (struct.error, IndexError):
        return None


def query(name: str, server: str, timeout: float = DNS_TIMEOUT_S) -> list[str] | None:
    """Ask one resolver for the A records of name. None when it did not answer."""
    query_id = secrets.randbits(16)
    qname = b"".join(bytes([len(label)]) + label.encode("ascii")
                     for label in name.rstrip(".").split(".")) + b"\x00"
    packet = (struct.pack(">HHHHHH", query_id, 0x0100, 1, 0, 0, 0)
              + qname + struct.pack(">HH", _TYPE_A, _CLASS_IN))
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        try:
            sock.sendto(packet, (server, 53))
            data, _ = sock.recvfrom(4096)
        except OSError:
            return None
    return parse_response(data, query_id, name, server)


def resolve(name: str) -> tuple[list[str], str]:
    """IPv4 addresses for name and the resolver that gave them."""
    for server in PUBLIC_RESOLVERS:
        found = query(name, server)
        if found is None:
            continue
        if not found:
            raise _not_found(name, server)
        return found, server
    infos = socket.getaddrinfo(name, 443, socket.AF_INET, socket.SOCK_STREAM)
    return list(dict.fromkeys(info[4][0] for info in infos)), "system"


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS to `address` that presents and verifies `host` (SNI, Host header,
    certificate) exactly as if host had resolved to it."""

    def __init__(self, host: str, address: str, *, context: ssl.SSLContext, **kwargs):
        super().__init__(host, context=context, **kwargs)
        self._address = address
        self._tls = context

    def connect(self) -> None:
        sock = socket.create_connection((self._address, self.port), self.timeout)
        self.sock = self._tls.wrap_socket(sock, server_hostname=self.host)


def fetch(url: str, timeout: float = 10.0) -> dict:
    """GET url without following redirects. Raises when no HTTP answer comes
    back; the CLI turns that into status -1."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError(f"not an https URL: {url!r}")
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    addresses, resolver = resolve(parts.hostname)
    context = ssl.create_default_context()
    last: Exception | None = None
    for address in addresses:
        conn = _PinnedHTTPSConnection(parts.hostname, address, context=context,
                                      port=parts.port or 443, timeout=timeout)
        try:
            conn.request("GET", path, headers={"User-Agent": "catena-public-probe"})
            resp = conn.getresponse()
            body = resp.read(BODY_LIMIT)
            return {"status": resp.status, "location": resp.getheader("Location", ""),
                    "body": body.decode("utf-8", "replace"), "address": address,
                    "resolver": resolver, "error": ""}
        except OSError as e:
            last = e
        finally:
            conn.close()
    raise last if last else OSError(f"no address for {parts.hostname}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True)
    ap.add_argument("--timeout", type=float, default=10.0)
    args = ap.parse_args(argv)
    try:
        result = fetch(args.url, args.timeout)
    except Exception as e:  # noqa: BLE001 -- the reason is the payload
        result = {"status": -1, "location": "", "body": "", "address": "",
                  "resolver": "", "error": f"{type(e).__name__}: {e}"}
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
