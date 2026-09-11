"""Ansible filter: compare the bootstrap-owned files against what the last
converge saw.

    bootstrap_digests(finds, stats) -> {path: sha256}
    bootstrap_drift(previous, current) -> {added, removed, changed}

WHAT THIS IS FOR. boundary.yml lists the paths a reconcile may never WRITE --
the SSH trust path, the sudoers drop-in, the forced command, the kernel
hardening. The rule behind that list is "a reconcile may not modify anything
that would remove your ability to run a reconcile", and it is the right rule.

But it left those six paths with no reader at all. Nothing on the host looked
at them, so a change to any of them -- an edit, a package, an intrusion, a
half-finished bootstrap re-run -- was invisible until somebody went looking,
and the one thing that goes looking is an operator who already suspects
something.

NOT WRITING IS NOT THE SAME AS NOT NOTICING. This reports; it never repairs.
Repairing is bootstrap's, and that is question one of the boundary. What it
removes is the silence.

WHAT IT CANNOT TELL YOU. The baseline is what the last converge saw, so the
first converge on a host records whatever is there -- including, if it came to
that, a state that was already wrong. This detects CHANGE from that point, not
correctness, and saying otherwise would be the more dangerous of the two
mistakes. The honest use is "these files moved between converge N and N+1, and
nothing in the product moves them".

Deliberately NOT compared: the live kernel values behind /etc/sysctl.d. dockerd
writes its own at runtime and bootstrap/roles/host_hardening says so -- it
applies before dockerd's first start precisely so those writes do not win until
the next reboot. A live-vs-file check would therefore fire on every healthy
host, which is the fastest way to teach an operator to ignore a report.

End-to-end coverage: catena-ce ansible/tests/unit/test_bootstrap_drift.py
"""

from __future__ import annotations


def _digest_map(results) -> dict:
    """{path: sha256} out of whatever stat/find results are handed in.

    Accepts both shapes because both are needed: `find` returns a `files` list
    for the directories, `stat` returns one `stat` dict per single file."""
    out: dict = {}
    for result in (results or []):
        if not isinstance(result, dict):
            continue
        for entry in (result.get("files") or []):
            if isinstance(entry, dict) and entry.get("path"):
                out[str(entry["path"])] = str(entry.get("checksum") or "")
        info = result.get("stat")
        if isinstance(info, dict) and info.get("exists") and info.get("path"):
            out[str(info["path"])] = str(info.get("checksum") or "")
    return out


def bootstrap_digests(finds, stats) -> dict:
    """The current picture: every bootstrap-owned file and its digest."""
    merged = _digest_map(finds)
    merged.update(_digest_map(stats))
    return dict(sorted(merged.items()))


def bootstrap_drift(previous, current) -> dict:
    """What moved since the last converge looked.

    Three lists, kept apart because they mean different things. A REMOVED
    sudoers drop-in and an ADDED one are not the same event, and a report that
    merged them into "2 changes" would be one an operator has to re-derive
    before it is worth anything."""
    if not isinstance(previous, dict):
        previous = {}
    if not isinstance(current, dict):
        current = {}
    before = {str(k): str(v) for k, v in previous.items()}
    after = {str(k): str(v) for k, v in current.items()}

    return {
        "added": sorted(set(after) - set(before)),
        "removed": sorted(set(before) - set(after)),
        # An empty digest on either side means "recorded, but the content was
        # not readable". Reporting that as a change every converge would be
        # noise about the reader rather than about the file, so it counts only
        # when both sides carry a digest and the two differ.
        "changed": sorted(p for p in (set(before) & set(after))
                          if before[p] and after[p] and before[p] != after[p]),
    }


class FilterModule:
    def filters(self):
        return {
            "bootstrap_digests": bootstrap_digests,
            "bootstrap_drift": bootstrap_drift,
        }
