#!/bin/sh
# Acquired by backup and auto-update timers; prevents concurrent runs.
#
# Usage:
#   acquire-lock.sh <lock_file> <timeout_sec> <service_name>
#
#   lock_file:    path to the lock file (created if missing)
#   timeout_sec:  seconds to wait before giving up (0 = no timeout)
#   service_name: human-readable name for logging (e.g., "catena-backup")
#
# Atomically acquires an exclusive lock file. Exit code:
#   0 -- lock acquired
#   1 -- timeout waiting for lock (should trigger systemd failure + dead-man alert)
#   2 -- bad arguments or filesystem error
#
# Once acquired, the lock is held by the shell process -- the caller
# remains responsible for releasing it (unlock, or just exit). Systemd
# Type=oneshot with a lock-acquire ExecStartPre is the standard pattern.

set -eu

if [ $# -ne 3 ]; then
    printf 'Usage: %s <lock_file> <timeout_sec> <service_name>\n' "$0" >&2
    exit 2
fi

LOCK_FILE="$1"
TIMEOUT="$2"
SERVICE_NAME="$3"

log() { printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

# Ensure the lock file's parent directory exists.
LOCK_DIR=$(dirname "$LOCK_FILE")
if ! mkdir -p "$LOCK_DIR" 2>/dev/null; then
    log "FATAL: cannot create lock dir ${LOCK_DIR}"
    exit 2
fi

elapsed=0
while true; do
    # Attempt to create the lock file atomically (O_CREAT | O_EXCL).
    # If the file exists, ln -f will skip (exit 1 from the shell builtin
    # we could use, but it's cleaner to use 'set -C' for the same effect).
    # We use a subshell with 'set -C' (noclobber) to atomically create.
    if (
        set -C
        printf '%s\n' "$$" > "$LOCK_FILE"
    ) 2>/dev/null; then
        log "${SERVICE_NAME}: lock acquired (waited ${elapsed}s)"
        # Lock file is now owned by this process. Caller is responsible
        # for cleanup (shell exits or trap).
        exit 0
    fi

    # Lock file exists. Check if the process that created it is still
    # running. If not, remove the stale lock and retry.
    if [ -f "$LOCK_FILE" ]; then
        old_pid=$(cat "$LOCK_FILE" 2>/dev/null || echo "")
        if [ -n "$old_pid" ]; then
            # /proc/<pid> exists iff the process is running.
            if ! kill -0 "$old_pid" 2>/dev/null; then
                log "${SERVICE_NAME}: removing stale lock (pid ${old_pid} not running)"
                rm -f "$LOCK_FILE"
                # Retry immediately.
                continue
            fi
        fi
    fi

    # Lock is held by an active process. Wait and retry.
    if [ "$TIMEOUT" -gt 0 ] && [ "$elapsed" -ge "$TIMEOUT" ]; then
        log "TIMEOUT: waiting for lock held by ${LOCK_FILE} after ${elapsed}s"
        exit 1
    fi

    elapsed=$((elapsed + 1))
    sleep 1
done
