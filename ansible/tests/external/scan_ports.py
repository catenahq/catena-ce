#!/usr/bin/env python
"""
Pure-Python TCP connect scanner. No nmap, no root, no system deps beyond
the Python stdlib -- runs inside the repo's uv venv.

Used by tests/external/public-ports.yml to prove the VPS's public IP has no
listening ports. Connect-scan is enough: if a TCP handshake completes on a
port, something is listening publicly, which is the failure mode we care about.

Usage (validate.yml invokes this for you):
    python3 tests/external/scan_ports.py --host 203.0.113.10
    python3 tests/external/scan_ports.py --host 203.0.113.10 --ports 1-1024
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys


async def _probe(host: str, port: int, sem: asyncio.Semaphore, timeout: float) -> int | None:
    async with sem:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=timeout,
            )
        except (asyncio.TimeoutError, OSError):
            return None
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
        return port


async def _scan(host: str, ports: list[int], concurrency: int, timeout: float) -> list[int]:
    sem = asyncio.Semaphore(concurrency)
    tasks = [asyncio.create_task(_probe(host, p, sem, timeout)) for p in ports]
    results = await asyncio.gather(*tasks)
    return sorted(p for p in results if p is not None)


def _parse_ports(spec: str) -> list[int]:
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            out.extend(range(int(lo), int(hi) + 1))
        else:
            out.append(int(part))
    if not all(0 < p < 65536 for p in out):
        raise ValueError("ports must be in 1..65535")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", required=True, help="Target IP or hostname")
    ap.add_argument("--ports", default="1-65535", help="Port spec: 22,80,443 or 1-1024")
    ap.add_argument("--concurrency", type=int, default=500)
    ap.add_argument("--timeout", type=float, default=1.0, help="Per-port connect timeout, seconds")
    args = ap.parse_args()

    ports = _parse_ports(args.ports)
    open_ports = asyncio.run(_scan(args.host, ports, args.concurrency, args.timeout))

    json.dump({"host": args.host, "scanned": len(ports), "open_ports": open_ports}, sys.stdout)
    sys.stdout.write("\n")
    # Exit 0 regardless -- the caller decides what "open_ports" is acceptable.
    # This script reports; it does not judge.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
