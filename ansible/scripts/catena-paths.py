#!/usr/bin/env python3
"""Print every path this host's Catena owns, grouped by the restore boundary.

The decision logic is helpers/host_paths.py, which is pure and unit-tested.
This is the thin part: find the two manifests, read them, print.

    catena-paths            the grouped listing
    catena-paths --json     the same grouping as JSON, for the panel

Runs as any user. It names paths and never reads their contents, and it does
not need the config store -- "where does this host keep its things" has to be
answerable on a host whose store is the problem.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# Same resolution order the other lane scripts use, and for the same reason:
# the payload ships the modules, and a checkout runs from the tree.
for _d in (os.path.dirname(os.path.abspath(__file__)),
           os.environ.get("CATENA_PAYLOAD_LIB", "/usr/local/lib/catena")):
    if _d not in sys.path:
        sys.path.insert(0, _d)

import host_paths as hp  # noqa: E402

PAYLOAD_MANIFEST = os.environ.get(
    "CATENA_PAYLOAD_MANIFEST", "/var/lib/catena/payload-manifest.json")
CONVERGE_MANIFEST = os.environ.get(
    "CATENA_CONVERGE_MANIFEST", "/var/lib/catena/converge-manifest.json")
DATA_PREFIX = os.environ.get("CATENA_DATA_PREFIX", "/srv/catena")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true",
                    help="print the grouping as JSON rather than as a listing")
    args = ap.parse_args(argv)

    paths = hp.read_manifest(PAYLOAD_MANIFEST) + hp.read_manifest(CONVERGE_MANIFEST)
    grouped = hp.group(paths, DATA_PREFIX)
    if args.json:
        print(json.dumps({"data_prefix": DATA_PREFIX, "groups": grouped}, indent=2))
        return 0
    sys.stdout.write(hp.render(grouped, data_prefix=DATA_PREFIX))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
