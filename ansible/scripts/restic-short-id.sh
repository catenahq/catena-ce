#!/bin/sh
# catena-restic-short-id -- extract the short_id of the first
# snapshot in `restic snapshots --json` output piped on stdin.
#
# Why this exists:
#   `restic snapshots --json` emits COMPACT JSON on a single line:
#     [{"time":"...","tree":"...","short_id":"abc1234",...}]
#   The previous in-tree extractor used awk with `-F` doublequote and
#   a fixed field index. With compact JSON the whole array is one
#   record, so the fixed index landed on `time` -- not on the short_id
#   value. Stage-1d of the auto-update test bench then fed the time
#   string into `restic tag --add` and restic ignored it ("no matching
#   ID found for prefix") while exiting 0. The bug also lived in
#   auto-update-two-phase.sh::do_snapshot, meaning prod auto-update
#   was tagging timestamps (and silently failing to apply any tag)
#   for the entire life of the feature -- leaving the two-phase
#   rollback path with no tagged snapshot to find.
#
#   This single helper replaces the four duplicate extractors so
#   the regression cannot recur per-callsite.
#
# Usage:
#   restic snapshots --json --latest 1 | catena-restic-short-id
#   restic snapshots --tag X --json | catena-restic-short-id
#
# Exit codes:
#   0  short_id printed to stdout (trailing newline)
#   1  stdin had no snapshots (empty array, or no `"short_id":...`
#      key -- for restic versions that omit short_id in some modes)
#   2  no stdin / read error

set -u

if [ -t 0 ]; then
    echo "catena-restic-short-id: stdin is a tty; expected JSON" >&2
    echo "usage: restic snapshots --json | catena-restic-short-id" >&2
    exit 2
fi

# `grep -oE` prints just the matched substring; head -n1 takes only
# the first match (an array of N snapshots produces N matches);
# cut -d'"' -f4 lifts the value out of the captured `"short_id":"X"`
# token. Pipe failures (no match) collapse `short_id` to empty,
# which the post-pipe check converts into exit 1.
short_id=$(grep -oE '"short_id":"[^"]+"' | head -n 1 | cut -d'"' -f4)

if [ -z "${short_id:-}" ]; then
    exit 1
fi

printf '%s\n' "$short_id"
