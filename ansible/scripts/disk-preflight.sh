#!/bin/sh
# Shared disk-space preflight for backup, restore, and auto-update.
# Installed by roles/backup as /usr/local/bin/catena-disk-preflight.
#
# Usage: catena-disk-preflight <mountpoint> <min_bytes> [<context>]
#   - <mountpoint>   directory whose containing filesystem is checked
#   - <min_bytes>    integer; fail if free bytes are below this
#   - <context>      optional label included in the failure message
#
# Exit codes: 0 on success, 1 on insufficient free space or unparseable
# df output, 2 on argument error. Designed to be invoked under `set -e`
# from the backup wrapper, the auto-update driver, and Ansible's command
# module -- a non-zero exit interrupts the caller cleanly.
#
# /bin/sh, no Jinja, no awk -F'\t' surprises. df -Pk emits POSIX 1024-byte
# blocks; the available column is $4 on the data line.

set -eu

if [ "$#" -lt 2 ]; then
    printf 'usage: %s <mountpoint> <min_bytes> [<context>]\n' "$0" >&2
    exit 2
fi

MOUNT="$1"
MIN_BYTES="$2"
CONTEXT="${3:-disk preflight}"

case "$MIN_BYTES" in
    ''|*[!0-9]*)
        printf '%s: min_bytes must be a non-negative integer, got %s\n' \
            "$CONTEXT" "$MIN_BYTES" >&2
        exit 2
        ;;
esac

if [ ! -d "$MOUNT" ]; then
    printf '%s: mountpoint %s does not exist\n' "$CONTEXT" "$MOUNT" >&2
    exit 1
fi

# df -P forces POSIX output (no line-wrapping on long device paths);
# -k forces 1024-byte blocks regardless of locale BLOCKSIZE. The data
# line is the last line; column 4 is "Available".
AVAIL_KB=$(df -Pk "$MOUNT" 2>/dev/null | awk 'NR>1 { avail=$4 } END { print avail }')
case "$AVAIL_KB" in
    ''|*[!0-9]*)
        printf '%s: could not parse df output for %s\n' "$CONTEXT" "$MOUNT" >&2
        exit 1
        ;;
esac
AVAIL_BYTES=$((AVAIL_KB * 1024))

if [ "$AVAIL_BYTES" -lt "$MIN_BYTES" ]; then
    avail_h=$(awk -v b="$AVAIL_BYTES" 'BEGIN{
        if (b >= 1073741824) printf "%.2fG", b/1073741824;
        else if (b >= 1048576) printf "%.1fM", b/1048576;
        else if (b >= 1024) printf "%.1fK", b/1024;
        else printf "%dB", b;
    }')
    min_h=$(awk -v b="$MIN_BYTES" 'BEGIN{
        if (b >= 1073741824) printf "%.2fG", b/1073741824;
        else if (b >= 1048576) printf "%.1fM", b/1048576;
        else if (b >= 1024) printf "%.1fK", b/1024;
        else printf "%dB", b;
    }')
    printf '%s: %s has only %s free, need at least %s\n' \
        "$CONTEXT" "$MOUNT" "$avail_h" "$min_h" >&2
    exit 1
fi

exit 0
