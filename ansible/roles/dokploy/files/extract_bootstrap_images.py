#!/usr/bin/env python3
"""Extract literal container image refs from a Dokploy install.sh.

Why this exists: Dokploy bootstraps Postgres + Redis via
`docker service create`. Docker Swarm resolves each image's digest on the
MANAGER by querying the registry directly -- it does NOT honor the
daemon's `registry-mirrors` (those only cover the engine pull path). On
the test bench many fresh installs share one egress IP, so that anonymous,
mirror-bypassed digest lookup exhausts the docker.io pull quota and
`service create` hangs until the 15-min ceiling (rc=124, "image
postgres:16 could not be accessed on a registry to record its digest" ->
nodes loop "No such image").

Pre-pulling these images through the daemon (which DOES use the
authenticated mirror + persistent cache) puts them in the node local
store first; swarm then falls back to per-node tag resolution against the
local image and the manager digest warning is non-fatal.

We extract the refs from the exact install.sh about to run rather than
hardcoding them, so a Dokploy version bump that changes the bootstrap
images is picked up automatically. Only literal name:tag tokens are
emitted; the dokploy image itself uses `${VERSION_TAG}` (tag does not
start alphanumeric, so it is skipped) and is engine-pulled by install.sh
anyway. Prints one image ref per line.
"""

from __future__ import annotations

import re
import sys

# A name:tag token. The tag must start with an alphanumeric, which excludes
# `${VERSION_TAG}` (starts with `$`). The path part allows registry host,
# namespace, and repo segments.
_IMAGE_REF = re.compile(r"[a-z0-9][a-z0-9._/-]*:[a-zA-Z0-9][\w.-]*")


def extract(text: str) -> list[str]:
    """Return de-duplicated literal image refs, source order preserved."""
    out: list[str] = []
    seen: set[str] = set()
    for match in _IMAGE_REF.finditer(text):
        ref = match.group(0)
        # host:port pairs (443:443, 80:80) -- not images.
        if re.fullmatch(r"\d+:\d+", ref):
            continue
        # bind-mount specs (var/run/docker.sock:ro).
        if "docker.sock" in ref:
            continue
        if ref not in seen:
            seen.add(ref)
            out.append(ref)
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: extract_bootstrap_images.py <install.sh path>", file=sys.stderr)
        return 2
    with open(argv[1], encoding="utf-8", errors="replace") as handle:
        text = handle.read()
    for ref in extract(text):
        print(ref)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
