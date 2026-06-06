#!/bin/bash
# Prepare a portable tarball of the latest restic snapshot for off-host
# download. Emits a single gzip tar stream -- restic's `dump` walks the
# snapshot and tars stdout, so we pipe straight to gzip without an
# uncompressed intermediate on disk.
#
# Wired to an OliveTin button ("Export latest snapshot") so operators
# and clients (admin-group) can prepare a transferable copy of their
# data on demand -- matches the portability story behind restore.yml.
#
# The S3 repo itself is also directly accessible with the restic
# password (documented in client self-restore guide); this button is
# the no-S3-tooling path for operators who just want `scp` bytes off.
#
# Installed by roles/backup (ansible.builtin.copy, NOT .template). All
# per-host values come from the env file at $1 (default
# /etc/catena/backup.env).

set -euo pipefail

BACKUP_ENV="${1:-/etc/catena/backup.env}"

if [ ! -r "$BACKUP_ENV" ]; then
    echo "error: $BACKUP_ENV missing -- roles/backup/install.yml must have run first." >&2
    exit 1
fi

# set -a exports any vars assigned by backup.env (RESTIC_REPOSITORY +
# AWS_* + our BACKUP_* knobs).
set -a
# shellcheck disable=SC1090
. "$BACKUP_ENV"
set +a

EXPORT_DIR="${BACKUP_EXPORT_DIR:?BACKUP_EXPORT_DIR not set in $BACKUP_ENV}"
KEEP_LAST="${BACKUP_EXPORT_KEEP_LAST:-1}"
PASSFILE="${BACKUP_PASSWORD_FILE_PATH:?BACKUP_PASSWORD_FILE_PATH not set in $BACKUP_ENV}"
SCP_HOST="${BACKUP_SCP_HINT_HOST:-<host>}"

if [ ! -r "$PASSFILE" ]; then
    echo "error: $PASSFILE missing." >&2
    exit 1
fi

# Minimum free space required before starting. Export is streaming, so
# we only need room for the final tar.gz; pick a floor that avoids
# filling / during the stream.
MIN_FREE_MB=1024

mkdir -p "$EXPORT_DIR"

free_mb=$(df -Pm "$EXPORT_DIR" | awk 'NR==2 {print $4}')
if [ "${free_mb:-0}" -lt "$MIN_FREE_MB" ]; then
    echo "error: only ${free_mb}MB free under $EXPORT_DIR (need >= ${MIN_FREE_MB}MB)." >&2
    echo "Free disk or set backup_export_dir to a larger filesystem." >&2
    exit 1
fi

# ─── rotate: delete older exports, keeping last N ───────────────────────
if [ "$KEEP_LAST" -gt 0 ]; then
    find "$EXPORT_DIR" -maxdepth 1 -name 'snapshot-*.tar.gz' -printf '%T@\t%p\n' \
        | sort -nr \
        | awk -v k="$KEEP_LAST" 'NR>k {print $2}' \
        | xargs -r rm -f --
fi

# ─── stream restic dump -> gzip ──────────────────────────────────────────
ts=$(date -u +%Y%m%dT%H%M%SZ)
out="$EXPORT_DIR/snapshot-${ts}.tar.gz"
tmp="$out.partial"

export RESTIC_PASSWORD_FILE="$PASSFILE"

echo "Streaming restic latest snapshot -> $out"
echo "(tar created via 'restic dump latest /', piped through gzip; no intermediate extract)"

if ! restic dump latest / 2>/tmp/snapshot-export.err | gzip -c > "$tmp"; then
    rm -f "$tmp"
    echo "error: restic dump or gzip failed. stderr:" >&2
    cat /tmp/snapshot-export.err >&2 || true
    exit 1
fi
mv -f "$tmp" "$out"
rm -f /tmp/snapshot-export.err

size_h=$(du -h "$out" | awk '{print $1}')
echo
echo "✓ Export ready:"
echo "    $out"
echo "    size: $size_h"
echo
echo "Download off-host:"
echo "    scp ops@${SCP_HOST}:$out ."
echo
echo "Contents are a gzip tar of the snapshot root (extract with:"
echo "    tar -tzf <file> | head    # list"
echo "    tar -xzf <file>           # extract"
echo ")."
echo
echo "Keeping last $KEEP_LAST export(s) under $EXPORT_DIR; older ones removed."

# F6: re-render recovery.<zone>'s landing page so the new tarball
# appears in the exports table immediately. Non-fatal if the helper
# is missing or hits an error -- the export itself already succeeded.
if [ -n "${BACKUP_SNAPSHOT_LIST_SCRIPT:-}" ] && [ -x "${BACKUP_SNAPSHOT_LIST_SCRIPT}" ]; then
    "${BACKUP_SNAPSHOT_LIST_SCRIPT}" "${BACKUP_ENV}" || \
        echo "warning: snapshot-list refresh failed; non-fatal" >&2
fi
