#!/usr/bin/env python3
"""Name a catena-ce tree by the hash of what a catena-admin image ships of it.

A catena-admin image carries the catena-ce tree it was built with, and records
that tree's hash in VENDOR.json beside it. catena-admin
scripts/vendor-catena-ce.sh copies the files this module lists and records the
hash this module computes, and reconcile/roles/payload compares the tree
running a converge with the record of the image its engines come from. One
implementation on both sides, so the two cannot disagree about what a tree is.

What ships: the files git tracks under ansible/, minus ansible/tests/, with
their bytes on disk. The hash is the sha256 of one line per file, sorted by
path: `<path>\\0<sha256 of its content>\\n`, the path relative to the tree root.

A tree with no git checkout is one staged out of an image (catena-admin
catena-converge): it carries the image's VENDOR.json beside ansible/, and its
hash is the one recorded there.

Usage:
  tree_hash.py <tree root>            {"tree_sha256": ..., "describe": ...}
  tree_hash.py --sha256 <tree root>   the hash alone
  tree_hash.py --files <tree root>    the shipped paths, NUL-separated
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

SHIPPED = "ansible"
NOT_SHIPPED = "ansible/tests/"
RECORD = "VENDOR.json"


def shipped_files(root: Path) -> list[str]:
    """The paths an image ships of the checkout at root, sorted."""
    out = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--", SHIPPED],
        check=True, capture_output=True).stdout.decode("utf-8")
    return sorted(p for p in out.split("\0")
                  if p and not p.startswith(NOT_SHIPPED))


def content_hash(root: Path, paths: list[str]) -> str:
    digest = hashlib.sha256()
    for rel in sorted(paths):
        file_sha = hashlib.sha256((root / rel).read_bytes()).hexdigest()
        digest.update(f"{rel}\0{file_sha}\n".encode("utf-8"))
    return digest.hexdigest()


def _describe(root: Path) -> str:
    r = subprocess.run(
        ["git", "-C", str(root), "describe", "--always", "--dirty", "--tags"],
        capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def identify(root: Path) -> dict[str, str]:
    """The tree's hash and version: computed for a git checkout, read from
    VENDOR.json for a tree staged out of an image."""
    if (root / ".git").exists():
        return {"tree_sha256": content_hash(root, shipped_files(root)),
                "describe": _describe(root)}
    record = json.loads((root / RECORD).read_text(encoding="utf-8"))
    return {"tree_sha256": str(record.get("tree_sha256") or ""),
            "describe": str(record.get("describe") or "")}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--sha256", action="store_true")
    mode.add_argument("--files", action="store_true")
    ap.add_argument("root", type=Path)
    args = ap.parse_args(argv)
    try:
        if args.files:
            sys.stdout.write("".join(p + "\0" for p in shipped_files(args.root)))
        elif args.sha256:
            print(identify(args.root)["tree_sha256"])
        else:
            print(json.dumps(identify(args.root)))
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"tree_hash: cannot name the tree at {args.root}: {exc}",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
