#!/bin/sh
# catena-restic-unmount -- counterpart to catena-restic-mount. Stops
# the auto-unmount timer + FUSE service unit and explicitly unmounts
# /mnt/restic-browse. Idempotent: safe to invoke when nothing is
# currently mounted (returns 0 with a "not mounted" message).
#
# Plain /bin/sh -- deployed via ansible.builtin.copy from
# roles/backup/tasks/install.yml. No Jinja markup.

set -eu

MOUNTPOINT=/mnt/restic-browse

systemctl stop catena-restic-browse-stop.timer 2>/dev/null || true
systemctl stop catena-restic-browse.service 2>/dev/null || true

if ! mountpoint -q "$MOUNTPOINT" 2>/dev/null; then
    echo "catena-restic-unmount: $MOUNTPOINT was not mounted."
    exit 0
fi

if command -v fusermount3 >/dev/null 2>&1; then
    fusermount3 -u "$MOUNTPOINT"
elif command -v fusermount >/dev/null 2>&1; then
    fusermount -u "$MOUNTPOINT"
else
    echo "catena-restic-unmount: neither fusermount3 nor fusermount available; cannot unmount cleanly." >&2
    exit 2
fi

echo "catena-restic-unmount: $MOUNTPOINT unmounted."
