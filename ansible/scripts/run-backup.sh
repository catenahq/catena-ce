#!/bin/sh
# Take one restic snapshot of the host: dump the databases, back up the
# declared paths, prune to the retention policy, ping the dead-man check.
#
# Installed by roles/backup and scheduled by catena-backup.timer (weekly;
# a tighter OnCalendar fails the converge via the backup_weekly_cap
# filter). Runnable on demand with `systemctl start
# catena-backup.service` or the catena-admin "Backup now" action.
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

# Retention, sourced after the main env file so it wins.
#
# Its own file because backup.env is written ONCE, deliberately: it holds the
# repository credentials and a converge must not overwrite a rotated secret.
# Retention has to be able to change, so it cannot live there. This file is
# rendered by `catena-schedule apply` from /etc/catena/config.json, which the
# admin panel writes -- one source, one writer, one reader.
#
# Absent means catena-schedule has never run here. Not fatal at this point:
# the prune below refuses when it ends up with nothing to keep, and that is a
# better place to fail than before the snapshot is even taken.
BACKUP_RETENTION_ENV="${BACKUP_RETENTION_ENV:-/etc/catena/backup-retention.env}"
BACKUP_RETENTION_CONFIGURED=0
if [ -r "$BACKUP_RETENTION_ENV" ]; then
    BACKUP_RETENTION_CONFIGURED=1
    # shellcheck disable=SC1090
    . "$BACKUP_RETENTION_ENV"
fi
set +a

log() { printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

# R24: two healthchecks per run. _SUCCESS_ url is pinged ONLY on a
# clean exit (sliding window catches "no successful run in 2x schedule"
# without paging on a single transient). _ATTEMPTED_ url is pinged on
# every start and on /fail for hard structural failures (pg_dumpall
# abort, restic config error). The operator-side grace on the
# "succeeded" check is what realises the "alarm on N consecutive
# misses" semantic; the "attempted" check is the immediate-page lane.
# hc_url inserts a Healthchecks signal suffix (/start, /fail) into the PATH,
# ahead of any query string.
#
# The auto-provisioned URLs end in "?create=1" (roles/backup/defaults), and
# these pings used to be a plain "${URL}${suffix}" concatenation. That produced
# ".../catena-backup-attempted?create=1/fail": the signal landed in the QUERY
# STRING, the path stayed the bare check, and Healthchecks recorded a SUCCESS.
# So every hard failure on the default configuration -- a pg_dumpall abort, an
# unreachable repo -- pinged the immediate-page lane as if the run had gone
# fine. The lane existed, was wired up, and reported the opposite of the truth.
hc_url() {
    _base="$1"
    _suffix="${2:-}"
    if [ -z "$_suffix" ]; then
        printf '%s' "$_base"
        return
    fi
    case "$_base" in
        *\?*) printf '%s%s?%s' "${_base%%\?*}" "$_suffix" "${_base#*\?}" ;;
        *)    printf '%s%s' "$_base" "$_suffix" ;;
    esac
}

ping_hc() {
    suffix="${1:-}"
    if [ -n "${BACKUP_HEALTHCHECK_URL:-}" ]; then
        curl -fsS -m 10 --retry 3 "$(hc_url "${BACKUP_HEALTHCHECK_URL}" "${suffix}")" >/dev/null 2>&1 || true
    fi
}

ping_hc_attempted() {
    suffix="${1:-}"
    if [ -n "${BACKUP_HEALTHCHECK_ATTEMPTED_URL:-}" ]; then
        curl -fsS -m 10 --retry 3 "$(hc_url "${BACKUP_HEALTHCHECK_ATTEMPTED_URL}" "${suffix}")" >/dev/null 2>&1 || true
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

# ─── 0b: store-driven runtime gate + credential override ─────────────────
# The on-box config store (/etc/catena/config.json) is the source of truth:
# catena-admin writes backup creds / repo / retention / enable / interval there
# with NO converge. Read them at RUNTIME (overriding the converge-written
# backup.env) so a value set in the panel takes effect on the next run. Then
# self-gate: a not-yet-configured or disabled or not-yet-due backup logs the
# event and exits CLEAN (0) -- it is not a failure, so no /fail ping fires.
# BACKUP_FORCE=1 (the catena-admin "Backup now" action) bypasses the enable +
# due gates but still requires valid creds.
STORE="${CATENA_CONFIG_STORE:-/etc/catena/config.json}"
if [ -r "$STORE" ] && command -v python3 >/dev/null 2>&1; then
    _store_get() {  # _store_get <section> <key>  -> value or empty
        python3 - "$STORE" "$1" "$2" <<'PY' 2>/dev/null || true
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)
v = (d.get(sys.argv[2]) or {}).get(sys.argv[3])
if v is not None:
    sys.stdout.write(str(v))
PY
    }
    _v=$(_store_get config BACKUP_RESTIC_REPO);          [ -n "$_v" ] && RESTIC_REPOSITORY="$_v"
    _v=$(_store_get secrets backup_s3_access_key); [ -n "$_v" ] && AWS_ACCESS_KEY_ID="$_v"
    _v=$(_store_get secrets backup_s3_secret_key); [ -n "$_v" ] && AWS_SECRET_ACCESS_KEY="$_v"
    # Retention is NOT read here. It used to be, from flat config.BACKUP_KEEP_*
    # keys, while backup.env.j2 also templated the same four values -- and
    # backup.env is written only-if-absent, so on any host that already had it
    # the converge's copy was dead and the store's copy was live. Two writers,
    # one of them silently ignored. It now has exactly one path:
    # config.json `backup_retention` -> catena-schedule -> the env file sourced
    # below.
    BACKUP_ENABLED=$(_store_get config BACKUP_ENABLED)
    BACKUP_MIN_INTERVAL_HOURS=$(_store_get config BACKUP_MIN_INTERVAL_HOURS)
    _store_pw=$(_store_get secrets backup_restic_password)
    if [ -n "$_store_pw" ]; then
        mkdir -p /run/catena && chmod 700 /run/catena
        _rt_pass=/run/catena/restic-runtime.pass
        ( umask 077; printf '%s\n' "$_store_pw" > "$_rt_pass" )
        RESTIC_PASSWORD_FILE="$_rt_pass"
    fi
    export RESTIC_REPOSITORY AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY RESTIC_PASSWORD_FILE
fi

# Credential completeness gate: stop if not fully configured -- and SAY SO on
# the attempted lane.
#
# This used to exit 0 before any ping. Combined with the check being
# provisioned on first successful ping, a host that had never been configured
# had no check at all: nothing to go late, nothing to alert, and the operator's
# only signal that a client has been running unbacked for weeks was noticing
# by hand. An unconfigured backup is not a quiet no-op, it is the most
# important thing to know about a host.
if [ -z "${RESTIC_REPOSITORY:-}" ] || [ -z "${AWS_ACCESS_KEY_ID:-}" ] \
        || [ -z "${AWS_SECRET_ACCESS_KEY:-}" ] \
        || [ ! -r "${RESTIC_PASSWORD_FILE:-/nonexistent}" ]; then
    log "backup not configured (missing restic repo / S3 creds / password); skipping. Set it in catena-admin > Settings > Backup."
    ping_hc_attempted /fail
    exit 0
fi

# Enable gate: only an EXPLICIT disable stops a configured host (unset -> on,
# so a converge that set creds keeps backing up as before). "Backup now" forces.
#
# Also /fail: a deliberately disabled backup is still a host with no backups,
# and the operator lane is where that belongs. Silence here is
# indistinguishable from a healthy host that simply has not run yet.
_enabled_lc=$(printf '%s' "${BACKUP_ENABLED:-true}" | tr '[:upper:]' '[:lower:]')
if [ "${BACKUP_FORCE:-0}" != "1" ] && [ "$_enabled_lc" = "false" ]; then
    log "backup disabled in catena-admin; skipping."
    ping_hc_attempted /fail
    exit 0
fi

# Due gate: an optional runtime cadence floor (hours between SUCCESSFUL runs)
# lets catena-admin lengthen the effective schedule beyond the base weekly
# timer with no converge. The systemd timer is the ceiling; this only skips
# runs firing sooner than the configured interval.
BACKUP_LAST_SUCCESS_STAMP="${BACKUP_LAST_SUCCESS_STAMP:-/var/lib/catena/backup-last-success}"
if [ "${BACKUP_FORCE:-0}" != "1" ] \
        && [ -n "${BACKUP_MIN_INTERVAL_HOURS:-}" ] \
        && [ "${BACKUP_MIN_INTERVAL_HOURS}" -gt 0 ] 2>/dev/null \
        && [ -f "$BACKUP_LAST_SUCCESS_STAMP" ]; then
    _age_s=$(( $(date +%s) - $(stat -c %Y "$BACKUP_LAST_SUCCESS_STAMP") ))
    _min_s=$(( BACKUP_MIN_INTERVAL_HOURS * 3600 ))
    if [ "$_age_s" -lt "$_min_s" ]; then
        log "backup not due yet (${_age_s}s since last success < ${_min_s}s configured interval); skipping."
        # /log, not /fail and not silence. Unlike the two gates above this one
        # is not a problem -- a recent success is why we are skipping, and the
        # "succeeded" check is still inside its grace window, so /fail here
        # would page on a healthy host. But it was the one guard in this file
        # that left NO trace, which makes "the timer fired and decided not to
        # run" and "the timer never fired" identical in the record. /log
        # records the event without touching the check's up/down state.
        ping_hc_attempted /log
        exit 0
    fi
fi

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

# ─── backend reachability preflight ──────────────────────────────────────
# restic has no connect-timeout: against an UNREACHABLE repo (endpoint down
# / firewalled so the TCP connect hangs), `restic unlock` and `restic
# backup` retry with backoff for ~20 min before giving up, silently wedging
# this oneshot -- and, in the bench, the whole run (fi_n7). Probe the
# backend up front with a HARD `timeout` bound so a hung endpoint fails in
# seconds. `restic cat config` is a lightweight read valid for every backend
# type. Gate ONLY on timeout (rc 124 = the probe hung = unreachable): a fast
# non-zero (e.g. a not-yet-initialised repo, a DNS error that returns
# immediately) is NOT a hang, so let the normal restic flow surface it
# rather than pre-failing a first-ever backup. The EXIT trap pings /fail.
BACKUP_REACH_TIMEOUT_S="${BACKUP_REACH_TIMEOUT_S:-45}"
log "backend reachability preflight (restic cat config, ${BACKUP_REACH_TIMEOUT_S}s bound)"
timeout "${BACKUP_REACH_TIMEOUT_S}" restic cat config >/dev/null 2>&1 || _reach_rc=$?
if [ "${_reach_rc:-0}" = 124 ]; then
    log "FATAL: restic backend did not respond within ${BACKUP_REACH_TIMEOUT_S}s"
    log "       (${RESTIC_REPOSITORY}). Failing fast instead of retrying for"
    log "       ~20 min. Check the S3 endpoint / network / firewall."
    exit 3
fi
unset _reach_rc 2>/dev/null || true

# Resolve the superuser for one postgres container. POSTGRES_USER from the
# live container env wins; BACKUP_PG_DUMP_USER is the fallback. A client app
# can ship a bespoke superuser, and pg_dumpall -U postgres would then error
# with "role does not exist". Used by BOTH the sizing probe below and the
# dump loop further down -- one implementation, so the two can never disagree
# about which role to connect as.
pg_superuser_for() {
    _pgu=$(docker exec "$1" sh -c 'printf %s "${POSTGRES_USER:-}"' 2>/dev/null \
             | tr -d '\r\n')
    if [ -z "$_pgu" ]; then
        _pgu="$BACKUP_PG_DUMP_USER"
    fi
    printf '%s' "$_pgu"
}

# Every running container whose image name contains "postgres". Works for any
# image derived from official postgres (pgvector, postgis, etc.). The `awk`
# form avoids docker --format Go-templates, which is the bit that kept biting
# us when this lived under Jinja.
pg_containers() {
    docker ps --no-trunc --format '{{.Names}}\t{{.Image}}' \
      | awk -F'\t' 'tolower($2) ~ /postgres/ {print $1}'
}

# ─── R16: disk-space preflight ───────────────────────────────────────────
# Abort the run before pg_dumpall writes a single byte if the staging
# mount is below the required free space. The EXIT trap above pings /fail,
# so an operator dead-man alert fires the same way it would for any other
# pre-restic failure.
#
# BACKUP_PREFLIGHT_MIN_BYTES is a FLOOR, not the answer. It was sized for
# "an SMB-stack VPS" and knows nothing about the tenant in front of it, so a
# host with a large Postgres passed this check and then filled the disk
# mid-dump -- caught only by the fatal-pg_dumpall path, after the backup
# window was burned and staging was left holding a partial file.
#
# So derive the real requirement from what is about to be written. pg_dumpall
# emits UNCOMPRESSED SQL to an intermediate file before gzip (see the
# two-step note in the dump loop), so peak staging demand tracks the logical
# size of the databases, not the size of the finished .sql.gz. Sum
# pg_database_size across every running postgres container and require that,
# scaled by BACKUP_PREFLIGHT_DB_PCT, whenever it exceeds the floor.
#
# FAIL-OPEN on purpose: a container that will not answer the size query is
# not a reason to refuse a backup. Any probe failure leaves the floor in
# place and says so, because a preflight that blocks on its own
# instrumentation is worse than the gap it closes.
BACKUP_PREFLIGHT_DB_PCT="${BACKUP_PREFLIGHT_DB_PCT:-100}"
_pf_min="${BACKUP_PREFLIGHT_MIN_BYTES:-0}"
_pf_db_total=0
if command -v docker >/dev/null 2>&1; then
    for _pf_c in $(pg_containers); do
        _pf_u=$(pg_superuser_for "$_pf_c")
        _pf_sz=$(docker exec "$_pf_c" psql -U "$_pf_u" -tAc \
                   'SELECT coalesce(sum(pg_database_size(datname)),0) FROM pg_database WHERE datallowconn' \
                   2>/dev/null | tr -d '\r\n ')
        case "$_pf_sz" in
            ''|*[!0-9]*)
                log "preflight sizing: ${_pf_c} did not answer the database-size query; that container is not counted"
                ;;
            *)
                _pf_db_total=$((_pf_db_total + _pf_sz))
                ;;
        esac
    done
fi
if [ "$_pf_db_total" -gt 0 ]; then
    _pf_need=$(( _pf_db_total * BACKUP_PREFLIGHT_DB_PCT / 100 ))
    if [ "$_pf_need" -gt "$_pf_min" ]; then
        log "preflight sizing: databases total $((_pf_db_total / 1048576)) MiB -> requiring $((_pf_need / 1048576)) MiB free (static floor was $((_pf_min / 1048576)) MiB)"
        _pf_min="$_pf_need"
    fi
fi

# Gate on the RESOLVED requirement, not on the static floor. Gating on
# BACKUP_PREFLIGHT_MIN_BYTES meant a host with no floor configured skipped the
# check even when the sizing probe had just measured its databases -- the
# derived number was computed and then thrown away. `-gt 0` also keeps the
# genuinely-empty case out: nothing to require is not a check worth running,
# it is a guaranteed pass dressed up as one.
if [ -n "${BACKUP_DISK_PREFLIGHT_SCRIPT:-}" ] \
        && [ -x "${BACKUP_DISK_PREFLIGHT_SCRIPT}" ] \
        && [ "$_pf_min" -gt 0 ]; then
    "${BACKUP_DISK_PREFLIGHT_SCRIPT}" \
        "${BACKUP_STAGING_DIR}" \
        "${_pf_min}" \
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
    CONTAINERS=$(pg_containers)
    if [ -n "$CONTAINERS" ]; then
        dump_failures=""
        for c in $CONTAINERS; do
            # Strip the swarm task suffix (`.<replica>.<task_id>`) from the
            # container name so the dump filename is stable across host
            # reschedules. catena-postgres is a swarm service whose live
            # name is `catena-postgres.1.<task_id>` -- that task_id changes
            # on every restart, and pg_replay's filename-based container
            # lookup would never match a freshly-rescheduled task. Plain
            # (compose-managed) containers like `nextcloud-db-1`
            # don't match the suffix pattern and pass through unchanged.
            name=$(printf '%s' "$c" | sed -E 's/\.[0-9]+\.[a-z0-9]+$//')

            # Per-container superuser, not a global default. Without pipefail
            # the `... | gzip` pipeline returns gzip's rc (0 on empty input),
            # so a wrong-role error gets silently committed as a 20-byte
            # empty-gzip "dump" and a future restore loses every row of that
            # database. See pg_superuser_for above (shared with the preflight
            # sizing probe).
            user=$(pg_superuser_for "$c")

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
        # Postgres data volumes ARE in the restic set: backup_paths lists
        # {{ storage_mount_point }}/docker/volumes, and roles/backup/defaults
        # says outright that catena-postgres-data is deliberately NOT
        # excluded. Both the infra DB and client DBs restore RAW; the dumps
        # are the cross-check and the partial-replay path
        # (`catena-recovery post-restore` replays scope=clients after the
        # stacks redeploy), not the sole copy.
        #
        # This comment used to claim the opposite -- that the volumes were
        # excluded and the dumps were the only source of truth. That is what
        # an operator reads while deciding how to recover, so it is worth
        # keeping exact.
        #
        # A dump failure is still FATAL, for a different reason than the old
        # comment gave: pg_dumpall failing means the database did not answer
        # or could not be read. Taking a snapshot of a volume whose engine is
        # in that state, and calling the run a success, is how a broken
        # database becomes a broken backup nobody looked at.
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

    # ─── mysqldump for each running MySQL/MariaDB container ──────────────
    # Seven catalog templates ship a MariaDB or MySQL (erpnext, wordpress,
    # kimai, espocrm, mautic, invoiceninja, easyappointments). Their data
    # volumes are in the restic set like every other volume, but until this
    # existed the raw volume was the ONLY copy: no logical dump to check it
    # against, and no way to restore one database without restoring the whole
    # volume. A raw copy of a running InnoDB directory is crash-consistent at
    # best, and "crash-consistent" is a thing you find out about during a
    # restore.
    #
    # Deliberately a SEPARATE directory from pg/. catena-recovery's replay
    # takes every *.sql.gz under backup-staging/pg and feeds it to psql; a
    # MariaDB dump landing there would be fed to the wrong engine on every
    # recovery.
    MYSQL_DIR="${BACKUP_STAGING_DIR}/mysql"
    mkdir -p "$MYSQL_DIR"
    chmod 700 "$MYSQL_DIR"
    find "$MYSQL_DIR" -type f -name '*.sql.gz' -mtime +3 -delete || true

    MYSQL_CONTAINERS=$(docker ps --no-trunc --format '{{.Names}}\t{{.Image}}' \
                         | awk -F'\t' 'tolower($2) ~ /mariadb|mysql/ {print $1}')
    if [ -n "$MYSQL_CONTAINERS" ]; then
        mysql_failures=""
        for c in $MYSQL_CONTAINERS; do
            name=$(printf '%s' "$c" | sed -E 's/\.[0-9]+\.[a-z0-9]+$//')
            ts=$(date -u +%Y%m%dT%H%M%SZ)
            out="${MYSQL_DIR}/${name}-${ts}.sql.gz"
            tmp="${MYSQL_DIR}/.${name}-${ts}.sql"
            log "mysqldump: ${c} -> ${out}"

            # The root password stays INSIDE the container. It is already in
            # that container's environment, so reading it there means it never
            # reaches this host's process table -- which `docker exec -e PW=...`
            # would, and which every user on the box can read.
            #
            # MariaDB 11 renamed the client binaries and ships mysqldump only as
            # a compatibility symlink that some images drop, so prefer
            # mariadb-dump and fall back. --single-transaction gives InnoDB a
            # consistent snapshot without locking the application out;
            # --routines --triggers --events keep the parts of a schema that are
            # not tables, whose absence only shows up when someone restores and
            # a report stops working.
            if docker exec "$c" sh -c '
                    set -e
                    MYSQL_PWD="${MARIADB_ROOT_PASSWORD:-${MYSQL_ROOT_PASSWORD:-}}"
                    export MYSQL_PWD
                    if [ -z "$MYSQL_PWD" ]; then
                        echo "no MARIADB_ROOT_PASSWORD/MYSQL_ROOT_PASSWORD in the container env" >&2
                        exit 3
                    fi
                    if command -v mariadb-dump >/dev/null 2>&1; then
                        DUMP=mariadb-dump
                    elif command -v mysqldump >/dev/null 2>&1; then
                        DUMP=mysqldump
                    else
                        echo "neither mariadb-dump nor mysqldump is in the image" >&2
                        exit 3
                    fi
                    exec "$DUMP" -u root --all-databases --single-transaction \
                        --routines --triggers --events
                ' 2>/tmp/mysql_dump.err > "$tmp"; then
                gzip -9 < "$tmp" > "$out"
                rm -f "$tmp"
            else
                log "mysqldump FAILED for ${c}:"
                sed 's/^/  /' /tmp/mysql_dump.err | head -40 >&2 || true
                rm -f "$tmp" "$out"
                mysql_failures="${mysql_failures} ${c}"
            fi
        done

        # Fatal for the same reason a pg_dumpall failure is: the dump failing
        # means the engine did not answer or could not be read, and snapshotting
        # its volume in that state while calling the run a success is how a
        # broken database becomes a broken backup nobody looked at.
        if [ -n "$mysql_failures" ]; then
            log "FATAL: mysqldump failed for:${mysql_failures}"
            log "       Refusing to proceed with restic backup -- missing"
            log "       dumps would silently lose data on restore."
            log "       Inspect /tmp/mysql_dump.err and the container log,"
            log "       fix the cause, then re-run the backup manually:"
            log "           systemctl start catena-backup.service"
            exit 2
        fi
    else
        log "no running MySQL/MariaDB containers found; skipping mysqldump"
    fi
else
    log "docker not installed; skipping pg_dumpall + mysqldump"
fi

# ─── clear stale backend locks ───────────────────────────────────────────
# catena-auto-update.service can be SIGKILL'd mid-`restic restore`
# (operationally during crash recovery; bench scenarios reproduce
# this in auto_update_mid_crash). A killed restic process never gets
# to release its S3-side lock, so the NEXT restic operation against
# the same repo refuses with "repository is already locked".
#
# Safe to --remove-all here: the unit's flock (R21) already
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
# Five buckets, each set independently in the admin panel, each passed
# through only when it is non-zero. Zero and omitted mean the same thing to
# restic -- keep nothing on that basis -- and omitting it keeps the command
# line from mentioning a bucket the operator chose not to have.
#
# The composition is the point, and it is why these are five numbers rather
# than a tier name: keep-last 8 with keep-hourly 24 on a fifteen-minute
# schedule holds eight snapshots across the last two hours and twenty-two
# more across the rest of the day. A bucket is also harmless on a host whose
# cadence never fills it -- forget is a no-op against snapshots that do not
# exist -- so a slower schedule needs no matching retention edit.
keep_args=""
add_keep() {
    # $1 flag, $2 value. Anything non-numeric is treated as unset rather
    # than passed through: restic would reject it and take the whole run
    # down at the prune, after the snapshot it was meant to protect.
    case "${2:-}" in
        ''|*[!0-9]*) return 0 ;;
    esac
    [ "$2" -gt 0 ] || return 0
    keep_args="${keep_args} $1 $2"
}
add_keep --keep-last "${BACKUP_KEEP_LAST:-}"
add_keep --keep-hourly "${BACKUP_KEEP_HOURLY:-}"
add_keep --keep-daily "${BACKUP_KEEP_DAILY:-}"
add_keep --keep-weekly "${BACKUP_KEEP_WEEKLY:-}"
add_keep --keep-monthly "${BACKUP_KEEP_MONTHLY:-}"

if [ -z "$keep_args" ] && [ "$BACKUP_RETENTION_CONFIGURED" -eq 0 ]; then
    # No retention file at all: nobody has ever set a policy on this host.
    # That is the state of every host between its first converge and the
    # first time the panel writes one, and it is NOT an error -- there is
    # nothing to prune against yet, and the snapshot this run just took is
    # the thing worth keeping. Skip the prune and finish clean; failing here
    # would fail the install itself.
    log "no retention policy configured yet ($BACKUP_RETENTION_ENV absent);"
    log "snapshot taken, skipping forget/prune. Set retention in the Catena"
    log "admin panel to start reclaiming space."
elif [ -z "$keep_args" ]; then
    # The file EXISTS and every bucket in it is zero or unset. That asks
    # restic to forget every snapshot in the repository, so it is a typo or
    # a hand-edit rather than an unconfigured host, and running the prune
    # would destroy the backups on the strength of it.
    log "FATAL: every retention bucket is zero or unset, which would prune"
    log "       every snapshot in the repository. Refusing. The snapshot"
    log "       taken by this run is safe; set retention in the Catena admin"
    log "       panel, or BACKUP_KEEP_* in $BACKUP_RETENTION_ENV, then re-run."
    exit 5
else

log "restic forget --prune (${keep_args# })"
# --keep-tag decommission: the final archival snapshot decommission.yml
# takes (tagged "decommission" via the systemd drop-in) is the "we tore the
# host down but still need the data" safety net. Time-based retention alone
# would prune it once it ages out of the hourly/daily/weekly/monthly
# windows -- so preserve any decommission-tagged snapshot unconditionally.
# (In the bench this is also what lets decommission_recovery still resolve
# the tagged snapshot after other scenarios' backups run forget on the
# shared repo.)
#
# Unquoted on purpose: keep_args is a built argv, not one word. Every value
# in it went through add_keep, which admits digits only.
# shellcheck disable=SC2086
restic forget \
    --keep-tag decommission \
    $keep_args \
    --prune --quiet
fi

# ─── stats JSON for Homepage widget ──────────────────────────────────────
# Runs AFTER retention so size reflects post-prune state. Parsed by the
# Homepage customapi widget via the nginx sidecar on catena-network.
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
# Catches the class of bug where a client compose uses an absolute bind-mount
# source outside backup_paths, so an application writes to a path no snapshot
# covers. Output goes to the journal; the admin panel runs the same script
# on demand.
#
# This runs AFTER the restic backup on purpose, and it is the reason the check
# can be fatal at all. The snapshot is already taken, so failing here does not
# cost the run its data: everything covered is captured, and the operator is
# paged about what is not. Failing BEFORE the backup would trade "some data
# unbacked" for "no data backed up", which is not an improvement.
#
# Exit 2 from the script means uncovered paths were found. Any other non-zero
# -- 3 when the covered-prefix list is missing, 124 from the timeout -- means
# the check itself could not run, and that stays non-fatal: an unreadable
# answer is not evidence of a gap. It is not evidence of coverage either,
# which is why "could not run" stopped being exit 0.
if [ -n "${BACKUP_COVERAGE_SCRIPT:-}" ] && [ -x "${BACKUP_COVERAGE_SCRIPT}" ]; then
    log "running backup coverage check"
    # `timeout` so a wedged `docker inspect` inside the coverage script
    # (it inspects every running container) can never hang this oneshot.
    # A HANG is not caught by an exit-code check, so without the bound a stuck
    # inspect leaves catena-backup.service in 'activating' until the caller's
    # timeout. 120s is ample for a host's container set.
    #
    # The rc is captured before the pipe: this script does not run with
    # pipefail, so `cmd | sed` would report sed's rc, which is always 0.
    _cov_out=$(timeout 120 "${BACKUP_COVERAGE_SCRIPT}" 2>&1) || _cov_rc=$?
    printf '%s\n' "$_cov_out" | sed 's/^/  coverage: /'
    if [ "${_cov_rc:-0}" = 2 ]; then
        log "FATAL: application data is being written outside the backup set."
        log "       The snapshot above completed and covers everything else."
        log "       The paths listed as UNCOVERED are not in any snapshot and"
        log "       will not come back from a restore. Move them under a"
        log "       covered prefix or add them to backup_paths, then re-run:"
        log "           systemctl start catena-backup.service"
        exit 4
    elif [ "${_cov_rc:-0}" != 0 ]; then
        log "coverage checker errored or timed out (rc ${_cov_rc}); non-fatal"
    fi
    unset _cov_out _cov_rc 2>/dev/null || true
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
# Stamp the last-success time so the runtime due-gate (BACKUP_MIN_INTERVAL_HOURS)
# can lengthen the effective cadence with no converge.
mkdir -p "$(dirname "${BACKUP_LAST_SUCCESS_STAMP:-/var/lib/catena/backup-last-success}")" 2>/dev/null || true
: > "${BACKUP_LAST_SUCCESS_STAMP:-/var/lib/catena/backup-last-success}" 2>/dev/null || true
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
