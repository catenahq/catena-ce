"""Ansible filters: decide which files a previous converge installed and this
one no longer ships.

    converge_installed(declared, stats)  -> [{path, sha256}, ...]
    converge_withdrawn(previous, declared) -> [{path, sha256}, ...]

WHAT THIS IS FOR. The converge was copy-only. A unit, script or helper module
withdrawn from a later release was simply not copied, and the old one kept
running: a timer nobody ships any more, firing at a binary nobody builds any
more. It has happened at least once already -- `reconcile/roles/backup` still
carries a hand-written `state: absent` for catena-acquire-lock.sh, a lock
helper whose replacement made it spin to its full timeout on every unit that
still found it.

The payload installer solved the same problem for the files it writes
(catena-admin payload/installer/install-ee-payload.sh), and this is deliberately
the same shape, because the two share directories: /usr/local/bin/catena-* and
/etc/systemd/system/catena-* are written by both. Two manifests, each pruning
only what it wrote, each checking content before it deletes, compose correctly.
A glob would not: either side would reap the other's files.

TWO RULES, and the safety of the whole thing rests on them.

1. DELETE ONLY WHAT IS NO LONGER DECLARED. The previous manifest records what
   was installed; the current converge declares what the product ships. The
   difference is "withdrawn from the product" -- never "not installed on this
   host", which is what a feature being switched off looks like and is not a
   reason to delete anything. Those two are indistinguishable from the
   installed sets alone, which is why `declared` is recorded separately from
   `installed` and why the diff is taken against it.

2. DELETE ONLY WHAT IS STILL OURS. Every candidate carries the digest the
   previous install recorded, and the caller re-checks it against the file on
   disk. If anything else has written that path since -- the payload installer,
   an operator, a package -- the digest differs and the file is left alone.
   That rule needs no cross-repo knowledge: "still byte-for-byte what I wrote"
   is decidable on this host, from this host.

A path missing from `declared` is therefore never deleted, only unmanaged. That
direction is chosen: an incomplete declaration costs coverage, and the opposite
mistake costs a file that was in use.

End-to-end coverage: catena-ce ansible/tests/unit/test_converge_prune.py
"""

from __future__ import annotations


def _paths(declared) -> list[str]:
    """Normalize a declared list: strings only, stripped, no blanks, deduped,
    order preserved.

    Templated entries are how a role names a unit whose stem is a variable, and
    an undefined variable renders as an empty string rather than raising -- so
    a blank here means a declaration that resolved to nothing. Dropping it is
    what keeps an unresolved name out of the recorded set, where it would match
    nothing and quietly widen the withdrawn list by one."""
    if declared is None:
        return []
    if not isinstance(declared, (list, tuple)):
        raise ValueError("converge manifest: declared paths must be a list")
    seen: set[str] = set()
    out: list[str] = []
    for raw in declared:
        if not isinstance(raw, str):
            raise ValueError(
                f"converge manifest: declared path must be a string, got {raw!r}")
        path = raw.strip()
        if not path or path in seen:
            continue
        if not path.startswith("/"):
            raise ValueError(
                f"converge manifest: declared path must be absolute: {path!r}")
        seen.add(path)
        out.append(path)
    return out


def converge_installed(declared, stats) -> list[dict]:
    """The entries to record: every declared path that is on the host, with the
    digest it has right now.

    `stats` is the results list from a stat loop over the same declared paths,
    in the same order -- each item carrying `item` (the path), `stat.exists`
    and `stat.checksum`.

    A declared path that is absent is recorded as declared and NOT as
    installed, which is the distinction rule 1 rests on: a feature switched off
    on this host declares its files and installs none of them, and must not
    read as a withdrawal next time."""
    by_path: dict[str, str] = {}
    for result in (stats or []):
        if not isinstance(result, dict):
            continue
        path = (result.get("item") or "")
        if isinstance(path, dict):
            path = path.get("path", "")
        path = str(path).strip()
        info = result.get("stat") or {}
        if not path or not info.get("exists"):
            continue
        by_path[path] = info.get("checksum") or ""

    return [{"path": p, "sha256": by_path[p]}
            for p in _paths(declared) if p in by_path]


def converge_withdrawn(previous, declared) -> list[dict]:
    """What the previous converge installed and this one no longer declares.

    Each entry carries the digest the previous manifest recorded, so the caller
    can check the file is still the one it wrote before removing it. An
    entry whose recorded digest is empty is returned with an empty digest and
    the caller must refuse it: "I did not record what I wrote" is not a licence
    to delete whatever is there now."""
    if not isinstance(previous, dict):
        previous = {}
    keep = set(_paths(declared))
    out: list[dict] = []
    seen: set[str] = set()
    for entry in (previous.get("installed") or []):
        if not isinstance(entry, dict):
            continue
        path = str(entry.get("path") or "").strip()
        if not path or path in keep or path in seen:
            continue
        seen.add(path)
        out.append({"path": path, "sha256": str(entry.get("sha256") or "")})
    return sorted(out, key=lambda e: e["path"])


def _split(stats) -> tuple[list[dict], list[dict]]:
    """(still ours, someone else's) from a stat loop over withdrawn candidates.

    A candidate that no longer exists is in neither: there is nothing to delete
    and nothing to report, which is the ordinary outcome on a host that was
    converged twice."""
    ours: list[dict] = []
    foreign: list[dict] = []
    for result in (stats or []):
        if not isinstance(result, dict):
            continue
        entry = result.get("item")
        if not isinstance(entry, dict) or not entry.get("path"):
            continue
        info = result.get("stat") or {}
        if not info.get("exists"):
            continue
        recorded = str(entry.get("sha256") or "")
        # An entry whose digest was never recorded is NOT ours to delete. The
        # check is the whole safety rule, and "I did not record what I wrote"
        # is not a licence to remove whatever is at that path now.
        if recorded and recorded == str(info.get("checksum") or ""):
            ours.append(entry)
        else:
            foreign.append(entry)
    return ours, foreign


def converge_prunable(stats) -> list[dict]:
    """Withdrawn candidates that are still byte-for-byte what we wrote."""
    return _split(stats)[0]


def converge_foreign(stats) -> list[dict]:
    """Withdrawn candidates that exist but are somebody else's now.

    Reported rather than passed over: /usr/local/bin/catena-* and
    /etc/systemd/system/catena-* are shared with the panel payload, so a
    changed digest is a real answer about who owns that path -- and a converge
    that stayed quiet about it would make "left it alone" and "never looked at
    it" the same silence."""
    return _split(stats)[1]


class FilterModule:
    def filters(self):
        return {
            "converge_installed": converge_installed,
            "converge_withdrawn": converge_withdrawn,
            "converge_prunable": converge_prunable,
            "converge_foreign": converge_foreign,
        }
