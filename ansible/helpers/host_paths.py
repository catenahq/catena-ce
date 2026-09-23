#!/usr/bin/env python3
"""Every path this host's Catena owns, grouped by whether a restore brings it
back.

THE QUESTION THIS ANSWERS. Catena's files are spread across /srv, /etc,
/usr/local and /var/lib, and the spread is not sprawl: it IS the restore
boundary. `/srv/catena` means "this comes back on a data restore". `/etc`,
`/usr/local/bin`, `/usr/local/lib/catena` and `/var/lib/dpkg` belong to the
MACHINE rather than to the data, and restoring them over a live converged host
would replace its ssh host keys, its firewall state and its installed binaries
with another machine's. A third group is the operating system's own
configuration directories, which Catena writes into and which can never move.

So the answer to "put everything in one place" is not a move, it is this: the
host already knows what it owns, in two manifests, and until now nothing
printed their union.

  /var/lib/catena/payload-manifest.json    every path the payload wrote, with a
                                           sha256 per entry. It is the prune
                                           leg's input, so it is accurate
                                           rather than aspirational.
  /var/lib/catena/converge-manifest.json   what the last converge installed, so
                                           the next one can withdraw it.

Neither covers the OS-mandated group, because nothing wrote it as a unit -- the
files are individual drop-ins in directories the OS owns. That group is
DECLARED below, and it is the half an operator is least likely to find.

NAMES PATHS, NEVER CONTENTS. Nothing here reads a file's contents, and the
listing does not require the config store to be readable: "where does this host
keep its things" must be answerable on a host whose store is the problem.

Stdlib only -- it runs on the target beside the other lane scripts.
"""

from __future__ import annotations

import json
import os
import textwrap

# The three groups, in the order a reader wants them: what comes back, what
# does not, and what the operating system owns.
TIER_DATA = "data"
TIER_MACHINE = "machine"
TIER_OS = "os"

TIER_TITLES = {
    TIER_DATA: "Data -- a restore brings these back",
    TIER_MACHINE: "Machine state -- backed up, deliberately NOT restored",
    TIER_OS: "Operating-system configuration Catena writes into",
}

TIER_NOTES = {
    TIER_DATA: (
        "App data, the Docker data-root and the backup staging area. The one "
        "group a data restore replaces."
    ),
    TIER_MACHINE: (
        "In the snapshot and skipped by a restore: they belong to this "
        "machine, and another host's copies would replace its ssh host keys, "
        "its firewall state and its installed binaries. The exception is the "
        "config store, which ADOPT_CONFIG takes deliberately, by itself."
    ),
    TIER_OS: (
        "Drop-ins in directories the OS owns. They cannot move, and no "
        "manifest records them as a unit, so they are declared."
    ),
}

# The OS-mandated group. DECLARED rather than discovered: these are files in
# directories that belong to the distribution, and a glob over them would list
# the distribution's files as Catena's.
OS_MANAGED_PATHS = (
    "/etc/ssh/sshd_config.d/99-catena.conf",
    "/etc/sudoers.d/catena-admin-runner",
    "/etc/sysctl.d/99-catena-hardening.conf",
    "/etc/modprobe.d/99-catena-hardening.conf",
    "/etc/apt/sources.list.d/tailscale.list",
    "/etc/apt/sources.list.d/docker.list",
    "/usr/share/keyrings/tailscale-archive-keyring.gpg",
    "/usr/share/keyrings/docker.gpg",
    "/etc/fstab",
)

# Prefixes that decide a manifest entry's group. Longest match wins, so
# /var/lib/catena falls to MACHINE while a deeper data path could still be
# classified on its own.
_MACHINE_PREFIXES = (
    "/etc",
    "/usr/local/bin",
    "/usr/local/lib/catena",
    "/usr/local/share/catena",
    "/var/lib/catena",
    "/var/lib/dpkg",
)


def classify(path: str, data_prefix: str) -> str:
    """Which group a path belongs to.

    `data_prefix` is a parameter rather than a constant because the prefix is
    the product's layout and this module is the thing that reports it: reading
    it from somewhere else here would make the report and the layout two
    declarations that can disagree.
    """
    path = (path or "").strip()
    if not path:
        return TIER_MACHINE
    if path in OS_MANAGED_PATHS:
        return TIER_OS
    if path == data_prefix or path.startswith(data_prefix.rstrip("/") + "/"):
        return TIER_DATA
    for prefix in _MACHINE_PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            return TIER_MACHINE
    # Anything else a manifest names is still Catena's and is still not data.
    # Reporting it as machine state is the conservative answer: it says "do not
    # expect a restore to bring this back", which is true of everything outside
    # the data prefix.
    return TIER_MACHINE


def manifest_paths(raw: str) -> list[str]:
    """The paths a manifest records.

    Tolerant of both shapes the two manifests use -- a list of objects with a
    `path`, or a bare list of strings -- and of a file that is absent or
    unreadable, which is a host that has not run that installer yet rather than
    an error. A listing that refused to print anything because one input was
    missing would be least useful exactly when it is most needed.
    """
    try:
        doc = json.loads(raw or "")
    except ValueError:
        return []
    entries = doc.get("entries", doc) if isinstance(doc, dict) else doc
    if not isinstance(entries, list):
        return []
    out: list[str] = []
    for entry in entries:
        if isinstance(entry, str):
            candidate = entry
        elif isinstance(entry, dict):
            candidate = entry.get("path") or ""
        else:
            continue
        candidate = str(candidate).strip()
        if candidate:
            out.append(candidate)
    return out


def read_manifest(path: str) -> list[str]:
    try:
        with open(path, encoding="utf-8") as fh:
            return manifest_paths(fh.read())
    except OSError:
        return []


def group(paths: list[str], data_prefix: str) -> dict[str, list[str]]:
    """Sorted, deduped paths per group, with the OS-mandated set folded in."""
    grouped: dict[str, set[str]] = {
        TIER_DATA: set(), TIER_MACHINE: set(), TIER_OS: set()}
    for path in [*paths, *OS_MANAGED_PATHS]:
        grouped[classify(path, data_prefix)].add(path.strip())
    return {tier: sorted(values) for tier, values in grouped.items()}


def render(grouped: dict[str, list[str]], *, data_prefix: str) -> str:
    """The listing, as a client or an operator reads it."""
    lines = [
        f"Catena paths on this host (data prefix {data_prefix})",
        "",
    ]
    for tier in (TIER_DATA, TIER_MACHINE, TIER_OS):
        lines.append(TIER_TITLES[tier])
        lines.extend("  " + chunk
                     for chunk in textwrap.wrap(TIER_NOTES[tier], 74))
        lines.append("")
        entries = grouped.get(tier) or []
        if not entries:
            lines.append("  (none)")
        for path in entries:
            marker = " " if os.path.exists(path) else "?"
            lines.append(f"  {marker} {path}")
        lines.append("")
    lines.append("A `?` marks a path this host declares and that is not on "
                 "disk, which on a")
    lines.append("feature nobody enabled is the right answer rather than a "
                 "fault.")
    return "\n".join(lines) + "\n"
