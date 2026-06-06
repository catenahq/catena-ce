#!/bin/sh
# catena-restic-mount -- mount the restic repository read-only at
# /mnt/restic-browse so an operator can recover a single file or
# directory from a past snapshot without running a full restore.
#
# Use case: a client deleted a file in Nextcloud, the in-app trash
# was emptied, and the file needs to come back from last night's
# backup. Whole-host `restic restore` would replace every byte on
# the VPS; a FUSE mount lets the operator copy out one path.
#
# The mount is backed by a transient systemd unit
# (catena-restic-browse.service) so it survives the OliveTin SSH
# session that started it. A second unit
# (catena-restic-browse-stop.timer) auto-unmounts after the timeout
# argument (default 1h) so a forgotten browse session does not leave
# a FUSE mount and a long-lived restic process behind.
#
# Usage:
#   sudo /usr/local/bin/catena-restic-mount [TIMEOUT]
#
# TIMEOUT is any value accepted by `systemd-run --on-active=`
# (e.g. 30min, 1h, 2h). Defaults to 1h.
#
# Exit codes:
#   0 on success
#   1 if already mounted
#   2 on missing prerequisites (FUSE, restic-env)
#
# Plain /bin/sh -- deployed via ansible.builtin.copy from
# roles/backup/tasks/install.yml. No Jinja markup.

set -eu

MOUNTPOINT=/mnt/restic-browse
TIMEOUT="${1:-1h}"
RESTIC_ENV=/usr/local/bin/catena-restic-env

if [ ! -x "$RESTIC_ENV" ]; then
    echo "catena-restic-mount: $RESTIC_ENV not found; backup role not converged?" >&2
    exit 2
fi

if ! command -v fusermount3 >/dev/null 2>&1 && ! command -v fusermount >/dev/null 2>&1; then
    echo "catena-restic-mount: fusermount not found; install fuse3 (apt install fuse3) and re-run." >&2
    exit 2
fi

mkdir -p "$MOUNTPOINT"

if mountpoint -q "$MOUNTPOINT" 2>/dev/null; then
    echo "catena-restic-mount: already mounted at $MOUNTPOINT." >&2
    echo "Click 'Unmount snapshot browser' first, or wait for the auto-unmount timer." >&2
    exit 1
fi

# Stop any leftover units from a prior run that crashed without cleanup.
# Failures here are expected when the units were never created; suppress.
systemctl stop catena-restic-browse-stop.timer 2>/dev/null || true
systemctl stop catena-restic-browse.service 2>/dev/null || true

systemd-run \
    --unit=catena-restic-browse \
    --collect \
    --description="Read-only restic FUSE mount for recovery browse" \
    "$RESTIC_ENV" restic mount --no-lock "$MOUNTPOINT" >/dev/null

systemd-run \
    --on-active="$TIMEOUT" \
    --unit=catena-restic-browse-stop \
    --collect \
    --description="Auto-unmount catena-restic-browse after $TIMEOUT" \
    /usr/local/bin/catena-restic-unmount >/dev/null

# Wait a few seconds for the FUSE mount to come up before printing
# the path-hint message. restic spends 1-3s opening the repo cache.
i=0
while [ $i -lt 10 ]; do
    if mountpoint -q "$MOUNTPOINT" 2>/dev/null; then
        break
    fi
    sleep 1
    i=$((i + 1))
done

if ! mountpoint -q "$MOUNTPOINT" 2>/dev/null; then
    echo "catena-restic-mount: mount did not come up within 10s." >&2
    echo "Tail journalctl -u catena-restic-browse.service for the failure." >&2
    exit 1
fi

cat <<EOF
Restic repo mounted read-only at $MOUNTPOINT
  Latest snapshot:  $MOUNTPOINT/snapshots/latest/
  By short id:      $MOUNTPOINT/ids/<short-id>/
  All snapshots:    $MOUNTPOINT/snapshots/
  Tagged set:       $MOUNTPOINT/tags/<tag>/

Auto-unmount in $TIMEOUT (click 'Unmount snapshot browser' to release sooner).

Typical recovery (single file from latest snapshot):
  1. ls $MOUNTPOINT/snapshots/latest/mnt/data/docker/volumes/
  2. cp -a $MOUNTPOINT/snapshots/latest/<src> /mnt/data/docker/volumes/<dst>
  3. Restart the affected container from the Dokploy UI so it picks
     the file up.
EOF
