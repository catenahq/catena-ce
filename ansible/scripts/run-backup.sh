#!/bin/sh
# Installed by roles/backup. Run on demand: `systemctl start
# catena-backup.service` or the catena-admin "Backup now" action.
# Community edition ships no timer; wire your own cron if you want a
# schedule.
#
# Outline:
#   1. pg_dumpall every running postgres-ish container -> backup-staging/pg/
#   2. restic backup of paths in $BACKUP_PATHS_FILE (one per line),
#      excluding patterns in $BACKUP_EXCLUDE_FILE (one per line)
#   3. restic forget --prune for retention
#   4. optional healthcheck ping on start / success / failure
#      (external dead-man; skipped if BACKUP_HEALTHCHECK_URL is blank)
#
# All per-host values come from the env file at $1 (or
# /etc/catena/backup.env by default). This script is plain /bin/sh
# with NO Jinja markup -- deployed via ansible.builtin.copy.
#
# Exit codes: 0 on success, non-zero on failure (also pings /fail).

set -eu

ENV_FILE="${1:-/etc/catena/backup.env}"
# `set -a` auto-exports every key assigned by the env file. Production
# runs are systemd-launched with EnvironmentFile=$ENV_FILE so the keys
# are already in env, but ad-hoc invocations (e.g. `sudo run-backup.sh
# /tmp/alt.env`) come from a scrubbed sudo env and rely on this block.
# Same pattern enforced everywhere the project sources backup.env;
# see vps-scripts/restic-env.sh + tests/unit/test_restic_env_pattern.py.
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

log() { printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

# R24: two healthchecks per run. _SUCCESS_ url is pinged ONLY on a
# clean exit (sliding window catches "no successful run in 2x schedule"
# without paging on a single transient). _ATTEMPTED_ url is pinged on
# every start and on /fail for hard structural failures (pg_dumpall
# abort, restic config error). The operator-side grace on the
# "succeeded" check is what realises the "alarm on N consecutive
# misses" semantic; the "attempted" check is the immediate-page lane.
ping_hc() {
    suffix="${1:-}"
    if [ -n "${BACKUP_HEALTHCHECK_URL:-}" ]; then
        curl -fsS -m 10 --retry 3 "${BACKUP_HEALTHCHECK_URL}${suffix}" >/dev/null 2>&1 || true
    fi
}

ping_hc_attempted() {
    suffix="${1:-}"
    if [ -n "${BACKUP_HEALTHCHECK_ATTEMPTED_URL:-}" ]; then
        curl -fsS -m 10 --retry 3 "${BACKUP_HEALTHCHECK_ATTEMPTED_URL}${suffix}" >/dev/null 2>&1 || true
    fi
}

# R13: external dead-man fan-out. Pinged in parallel with the local
# succeeded URL at end-of-run on success only -- never on /start or
# /fail (the local attempted-lane handles immediate paging; external
# endpoints exist to detect WHOLE-HOST outage via missing pings, not
# to take immediate-fire signals from a half-dead host). Empty URLs
# are skipped silently; no ping = no error = same as today's behavior.
ping_hc_external() {
    if [ -n "${BACKUP_HEALTHCHECK_URL_CLIENT:-}" ]; then
        curl -fsS -m 10 --retry 3 "${BACKUP_HEALTHCHECK_URL_CLIENT}" >/dev/null 2>&1 || true
    fi
    if [ -n "${BACKUP_HEALTHCHECK_URL_OPERATOR:-}" ]; then
        curl -fsS -m 10 --retry 3 "${BACKUP_HEALTHCHECK_URL_OPERATOR}" >/dev/null 2>&1 || true
    fi
}

on_failure() {
    rc=$?
    log "FAILED with rc=${rc}"
    # /fail goes to the attempted check only -- operator pages
    # immediately on every failure via that lane. The succeeded check
    # silently misses its expected ping; sliding grace decides whether
    # this single miss is enough to alert (default: no -- paired with a
    # second consecutive miss, yes).
    ping_hc_attempted /fail
    exit "$rc"
}
trap on_failure EXIT

ping_hc_attempted /start
log "backup run starting"

# ─── R16: disk-space preflight ───────────────────────────────────────────
# Abort the run before pg_dumpall writes a single byte if the staging
# mount is below the configured floor. The EXIT trap above pings /fail,
# so an operator dead-man alert fires the same way it would for any other
# pre-restic failure.
if [ -n "${BACKUP_DISK_PREFLIGHT_SCRIPT:-}" ] \
        && [ -x "${BACKUP_DISK_PREFLIGHT_SCRIPT}" ] \
        && [ -n "${BACKUP_PREFLIGHT_MIN_BYTES:-}" ]; then
    "${BACKUP_DISK_PREFLIGHT_SCRIPT}" \
        "${BACKUP_STAGING_DIR}" \
        "${BACKUP_PREFLIGHT_MIN_BYTES}" \
        "backup preflight"
fi

# ─── pg_dumpall for each running postgres container ──────────────────────
PG_DIR="${BACKUP_STAGING_DIR}/pg"
mkdir -p "$PG_DIR"
chmod 700 "$PG_DIR"

# Retention on staging -- keep last 3 dumps per container; restic handles
# longer-term retention over in the repo.
find "$PG_DIR" -type f -name '*.sql.gz' -mtime +3 -delete || true

if command -v docker >/dev/null 2>&1; then
    # Any running container whose image name contains "postgres". Works
    # for any image derived from official postgres (pgvector, postgis,
    # etc.). The `awk` form avoids docker --format Go-templates, which
    # is the bit that kept biting us when this lived under Jinja.
    CONTAINERS=$(docker ps --no-trunc --format '{{.Names}}\t{{.Image}}' \
                   | awk -F'\t' 'tolower($2) ~ /postgres/ {print $1}')
    if [ -n "$CONTAINERS" ]; then
        dump_failures=""
        for c in $CONTAINERS; do
            # Strip the swarm task suffix (`.<replica>.<task_id>`) from the
            # container name so the dump filename is stable across host
            # reschedules. dokploy-postgres is a swarm service whose live
            # name is `dokploy-postgres.1.<task_id>` -- that task_id changes
            # on every restart, and pg_replay's filename-based container
            # lookup would never match a freshly-rescheduled task. Plain
            # (compose-managed) containers like `nextcloud-ehlkpl-db-1`
            # don't match the suffix pattern and pass through unchanged.
            name=$(printf '%s' "$c" | sed -E 's/\.[0-9]+\.[a-z0-9]+$//')

            # Detect the right superuser per container instead of trusting
            # BACKUP_PG_DUMP_USER as a global default. Dokploy's embedded
            # postgres initialises with POSTGRES_USER=dokploy, so role
            # `postgres` does NOT exist there -- pg_dumpall -U postgres
            # errors out with "role does not exist". Without pipefail the
            # `... | gzip` pipeline returns gzip's rc (0 on empty input),
            # so the error gets silently committed as a 20-byte empty-
            # gzip "dump" and a future restore loses every row of that
            # database. Read POSTGRES_USER from the live container env
            # and fall back to the operator-provided default only if it's
            # empty.
            user=$(docker exec "$c" sh -c 'printf %s "${POSTGRES_USER:-}"' 2>/dev/null \
                     | tr -d '\r\n')
            if [ -z "$user" ]; then
                user="$BACKUP_PG_DUMP_USER"
            fi

            ts=$(date -u +%Y%m%dT%H%M%SZ)
            out="${PG_DIR}/${name}-${ts}.sql.gz"
            log "pg_dumpall: ${c} (user=${user}) -> ${out}"
            # --clean --if-exists: emit DROP ... IF EXISTS stanzas so the
            # dump is idempotent on replay (drops the target DB / role
            # before recreating). Without this, pg_replay on top of a
            # fresh postgres container fails with "role already exists"
            # on the bootstrap role, or silently leaves stale rows.
            #
            # Two-step (intermediate file, then gzip) instead of a pipe:
            # `set -eu` provides no protection on a pipeline; the shell
            # exits on the rc of the LAST command, which is gzip -- and
            # gzip succeeds on empty input. Writing pg_dumpall's output
            # to a temp file first lets us check its real exit code and
            # fail loudly when it errors.
            tmp="${PG_DIR}/.${name}-${ts}.sql"
            if docker exec "$c" pg_dumpall -U "$user" \
                    --clean --if-exists 2>/tmp/pg_dump.err > "$tmp"; then
                gzip -9 < "$tmp" > "$out"
                rm -f "$tmp"
            else
                log "pg_dumpall FAILED for ${c} (user=${user}):"
                sed 's/^/  /' /tmp/pg_dump.err | head -40 >&2 || true
                rm -f "$tmp" "$out"
                dump_failures="${dump_failures} ${c}"
            fi
        done
        # Option A in the catena restore architecture: postgres data
        # volumes are NOT in the restic set (see roles/backup/defaults
        # comments). The logical dumps are the sole source of truth for
        # restoring a postgres container. If any dump fails, the backup
        # run must fail LOUDLY -- proceeding with a missing dump means a
        # future restore silently loses that container's data.
        if [ -n "$dump_failures" ]; then
            log "FATAL: pg_dumpall failed for:${dump_failures}"
            log "       Refusing to proceed with restic backup -- missing"
            log "       dumps would silently lose data on restore."
            log "       Inspect /tmp/pg_dump.err and the container log,"
            log "       fix the cause, then re-run the backup manually:"
            log "           systemctl start catena-backup.service"
            exit 2
        fi
    else
        log "no running postgres-ish containers found; skipping pg_dumpall"
    fi
else
    log "docker not installed; skipping pg_dumpall"
fi

# ─── clear stale backend locks ───────────────────────────────────────────
# catena-auto-update.service can be SIGKILL'd mid-`restic restore`
# (operationally during crash recovery; bench scenarios reproduce
# this in auto_update_mid_crash). A killed restic process never gets
# to release its S3-side lock, so the NEXT restic operation against
# the same repo refuses with "repository is already locked".
#
# Safe to --remove-all here: catena-acquire-lock.sh (R21) already
# holds /run/catena.lock, which is the exclusive local mutex shared
# with catena-auto-update.service. No concurrent catena-* process is
# touching this repo right now, so any backend lock we find IS stale
# by construction. Non-fatal: the unlock might fail transiently
# (network blip) -- restic backup will surface the real error if so.
log "restic unlock --remove-all (clear any stale lock from killed predecessor)"
restic unlock --remove-all || log "restic unlock failed (non-fatal); backup will retry"

# ─── restic backup ───────────────────────────────────────────────────────
# Build argv from the data files so the shell never sees a literal list.
#
# BACKUP_RESTIC_TAG defaults to "scheduled" (the timer-driven case).
# decommission.yml overrides to "decommission" via a systemd drop-in so
# the final archival snapshot is trivially filterable with
# `restic snapshots --tag decommission`. Any other caller that wants a
# tagged one-shot can set the env the same way.
RESTIC_TAG="${BACKUP_RESTIC_TAG:-scheduled}"
log "restic backup (tag=${RESTIC_TAG})"
set -- restic backup --tag "$RESTIC_TAG" --quiet

if [ -r "${BACKUP_PATHS_FILE:-/etc/catena/backup-paths}" ]; then
    while IFS= read -r line; do
        # strip comment + surrounding whitespace; skip blanks
        line="${line%%#*}"
        line=$(printf '%s' "$line" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')
        [ -z "$line" ] && continue
        set -- "$@" "$line"
    done < "${BACKUP_PATHS_FILE}"
else
    log "WARN: no paths file at ${BACKUP_PATHS_FILE:-/etc/catena/backup-paths}; running with zero paths"
fi

if [ -r "${BACKUP_EXCLUDE_FILE:-/etc/catena/backup-exclude-patterns}" ]; then
    while IFS= read -r line; do
        line="${line%%#*}"
        line=$(printf '%s' "$line" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')
        [ -z "$line" ] && continue
        set -- "$@" "--exclude" "$line"
    done < "${BACKUP_EXCLUDE_FILE}"
fi

"$@"

# ─── retention ───────────────────────────────────────────────────────────
# --keep-hourly added for the hourly-cadence default (BACKUP_SCHEDULE=
# hourly). Default 24 means one full day of hourly snapshots before
# the daily/weekly/monthly tail takes over; if an operator drops the
# cadence back to nightly, restic forget is a no-op against snapshots
# that do not exist, so the keep_hourly flag is harmless on a nightly
# host.
log "restic forget --prune"
restic forget \
    --keep-hourly "${BACKUP_KEEP_HOURLY:-24}" \
    --keep-daily "${BACKUP_KEEP_DAILY}" \
    --keep-weekly "${BACKUP_KEEP_WEEKLY}" \
    --keep-monthly "${BACKUP_KEEP_MONTHLY}" \
    --prune --quiet

# ─── stats JSON for Homepage widget ──────────────────────────────────────
# Runs AFTER retention so size reflects post-prune state. Parsed by the
# Homepage customapi widget via the nginx sidecar on dokploy-network.
# Failure here must NOT fail the whole backup run (widget is diagnostic,
# not load-bearing) -- hence the outer `|| log "..."` wrapper.
if [ -n "${BACKUP_STATS_FILE:-}" ]; then
    log "writing backup stats json -> ${BACKUP_STATS_FILE}"
    mkdir -p "$(dirname "${BACKUP_STATS_FILE}")"
    {
        STATS_JSON=$(restic stats --json --mode raw-data 2>/dev/null || echo '{}')
        TOTAL_SIZE=$(printf '%s' "$STATS_JSON" | awk -F'[,:]' '/total_size/{print $2+0; exit}')
        FILE_COUNT=$(printf '%s' "$STATS_JSON" | awk -F'[,:]' '/total_file_count/{print $2+0; exit}')
        # restic's `--latest 1` returns the latest snapshot per path-set
        # *group*, not one overall. After adding or removing a backup
        # path the repo has multiple groups, and awk-on-first-`time`
        # picks whichever group listed first -- usually the older one.
        # Parse the full snapshot list in Python and take the real max
        # by timestamp; also derive the count from the same list so we
        # don't invoke restic twice.
        ALL_SNAPS_JSON=$(restic snapshots --json 2>/dev/null || echo '[]')
        LAST_TIME=$(printf '%s' "$ALL_SNAPS_JSON" | python3 -c '
import json, sys
d = json.load(sys.stdin)
print(max((s.get("time", "") for s in d), default=""))
' 2>/dev/null)
        SNAP_COUNT=$(printf '%s' "$ALL_SNAPS_JSON" | python3 -c '
import json, sys
print(len(json.load(sys.stdin)))
' 2>/dev/null || echo 0)
        SIZE_HUMAN=$(awk -v b="$TOTAL_SIZE" 'BEGIN{
            if (b >= 1073741824) printf "%.2f GB", b/1073741824;
            else if (b >= 1048576) printf "%.1f MB", b/1048576;
            else if (b >= 1024) printf "%.1f KB", b/1024;
            else printf "%d B", b;
        }')
        cat > "${BACKUP_STATS_FILE}" <<JSON
{
  "status": "ok",
  "last_snapshot_at": "${LAST_TIME:-}",
  "repo_size_bytes": ${TOTAL_SIZE:-0},
  "repo_size_human": "${SIZE_HUMAN:-unknown}",
  "file_count": ${FILE_COUNT:-0},
  "snapshot_count": ${SNAP_COUNT:-0},
  "generated_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
JSON
        chmod 0644 "${BACKUP_STATS_FILE}"
    } || log "stats json generation hit an error; skipping (non-fatal)"
fi

# ─── coverage sanity check ───────────────────────────────────────────────
# Non-fatal -- catches the class of bug where a client compose uses an
# absolute bind-mount source outside backup_paths. Output goes to the
# journal; OliveTin has a button wired to the same script for on-demand.
if [ -n "${BACKUP_COVERAGE_SCRIPT:-}" ] && [ -x "${BACKUP_COVERAGE_SCRIPT}" ]; then
    log "running backup coverage check"
    # `timeout` so a wedged `docker inspect` inside the coverage script
    # (it inspects every running container) can never hang this oneshot.
    # The script is warn-only by contract; a HANG is not caught by the
    # `|| log` below (that only catches a non-zero EXIT), so without the
    # bound a stuck inspect leaves catena-backup.service in 'activating'
    # until the caller's timeout. 120s is ample for a host's container set.
    timeout 120 "${BACKUP_COVERAGE_SCRIPT}" 2>&1 | sed 's/^/  coverage: /' || \
        log "coverage checker errored or timed out; non-fatal"
fi

# ─── F6: refresh recovery.<zone> snapshot listing ────────────────────────
# Re-render ${BACKUP_EXPORT_DIR}/index.html so the recovery-downloads
# nginx sidecar shows the live repo state (current snapshots + pruned
# entries dropped) instead of stale rows. Non-fatal -- the page is
# diagnostic, not load-bearing.
if [ -n "${BACKUP_SNAPSHOT_LIST_SCRIPT:-}" ] && [ -x "${BACKUP_SNAPSHOT_LIST_SCRIPT}" ]; then
    log "regenerating snapshot-list page"
    # Same bound as the coverage tail: this runs `restic snapshots`, which
    # can stall on a network blip. Diagnostic page, must never block the run.
    timeout 120 "${BACKUP_SNAPSHOT_LIST_SCRIPT}" "${ENV_FILE}" 2>&1 | sed 's/^/  snapshot-list: /' || \
        log "snapshot-list errored or timed out; non-fatal"
fi

# ─── success ─────────────────────────────────────────────────────────────
trap - EXIT
log "backup run complete"
# Succeeded lane: clean run-end ping. Attempted lane: also mark this
# run "ok" so the attempted check stays green (it would otherwise still
# show /start as the last ping).
ping_hc
ping_hc_attempted
# R13: external dead-mans (client + operator) -- fan out the success
# ping so off-host monitors can detect whole-VPS outage via missing
# pings even when the local Healthchecks instance is down.
ping_hc_external
