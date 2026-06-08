#!/usr/bin/env bash
# client-tools/restore.sh -- disaster-recovery automation for clients.
#
# Drop-in replacement for the manual steps in self-restore.md. Run on a
# fresh VPS as root; the script prepares the host, restores the latest
# restic snapshot, installs Dokploy, replays per-app Postgres dumps,
# and optionally re-installs cloudflared with a new tunnel token.
#
# THIS SCRIPT MUST REMAIN SELF_CONTAINED
#
# Usage:
#
#   curl -fsSLo restore.sh https://<your-vps-docs>/restore.sh   # see docs
#   chmod +x restore.sh
#   sudo ./restore.sh
#
# The script prompts interactively for the credentials it needs. Every
# input can also be supplied via environment variable for non-interactive
# runs (cron, scripted recovery):
#
#   RESTIC_REPOSITORY        e.g. s3:s3.us-east-005.backblazeb2.com/acme-vps
#   RESTIC_PASSWORD          your restic encryption passphrase
#   AWS_ACCESS_KEY_ID        S3 access key for the backup bucket
#   AWS_SECRET_ACCESS_KEY    S3 secret key for the backup bucket
#   CLOUDFLARED_TOKEN        (optional) new tunnel token from Cloudflare
#   SKIP_DOKPLOY_INSTALL     (optional) "1" to skip the Dokploy install
#                            step (useful if Dokploy is already running)
#   DATA_DIR                 (optional) override for /mnt/data path
#
# Cold-bucket fallback (optional; supply when both hot and cold creds
# are in the recovery envelope, per the catena client self-service
# policy). When set, the script tries the hot bucket first and falls
# back to cold automatically if the hot bucket is unreachable -- the
# realistic ransomware-deleted-hot path:
#
#   RESTIC_REPOSITORY_COLD       e.g. s3:s3.ca-central-1.eazybackup.com/acme-cold
#   AWS_ACCESS_KEY_ID_COLD       cold-bucket access key
#   AWS_SECRET_ACCESS_KEY_COLD   cold-bucket secret key
#
# Nextcloud S3 primary-storage envelope (optional; supply when the VPS
# runs Nextcloud-on-external-S3). step_nextcloud_s3_repair probes the
# hot NC-S3 bucket and replays cold->hot when hot is empty/missing,
# preventing Nextcloud from coming back online with no user files:
#
#   NEXTCLOUD_S3_HOT_BUCKET      hot NC-S3 bucket name
#   NEXTCLOUD_S3_HOT_ENDPOINT    hot endpoint URL (e.g. https://...)
#   NEXTCLOUD_S3_HOT_ACCESS_KEY  hot key
#   NEXTCLOUD_S3_HOT_SECRET      hot secret
#   NEXTCLOUD_S3_COLD_BUCKET     cold NC-S3 bucket name
#   NEXTCLOUD_S3_COLD_ENDPOINT   cold endpoint URL
#   NEXTCLOUD_S3_COLD_ACCESS_KEY cold key
#   NEXTCLOUD_S3_COLD_SECRET     cold secret
#
# Self-contained on purpose: this script is the future single artifact
# downloaded from recovery.<zone>, password-encrypted by an OliveTin
# button at generation time. No sibling files, no runtime curl of
# extra catena assets. Bucket-mirror logic is embedded inline (see
# _emit_bucket_mirror_py); recovery-archive secrets enter via env.
#
# Stdlib-friendly: bash + curl + apt-get + restic + python3 (with
# boto3 installed via pip when NC-S3 cold replay is needed).
# Idempotent where possible: re-running after a partial failure picks
# up where it left off without re-restoring already-present data.

set -Eeuo pipefail

DATA_DIR="${DATA_DIR:-/mnt/data}"
SKIP_DOKPLOY_INSTALL="${SKIP_DOKPLOY_INSTALL:-0}"
LOG_PREFIX="[restore]"

# Cold-bucket fallback creds. Empty by default; populated by the
# operator handoff envelope when the client opts into self-service
# cold recovery. step_check_repo reads these to attempt a fallback
# when the hot bucket is unreachable.
RESTIC_REPOSITORY_COLD="${RESTIC_REPOSITORY_COLD:-}"
AWS_ACCESS_KEY_ID_COLD="${AWS_ACCESS_KEY_ID_COLD:-}"
AWS_SECRET_ACCESS_KEY_COLD="${AWS_SECRET_ACCESS_KEY_COLD:-}"

# Nextcloud S3 primary-storage envelope. Empty by default; populated
# when the VPS runs Nextcloud-on-external-S3 and the recovery archive
# carries both hot + cold NC-S3 coords. step_nextcloud_s3_repair runs
# only when a complete hot+cold quartet is present.
NEXTCLOUD_S3_HOT_BUCKET="${NEXTCLOUD_S3_HOT_BUCKET:-}"
NEXTCLOUD_S3_HOT_ENDPOINT="${NEXTCLOUD_S3_HOT_ENDPOINT:-}"
NEXTCLOUD_S3_HOT_ACCESS_KEY="${NEXTCLOUD_S3_HOT_ACCESS_KEY:-}"
NEXTCLOUD_S3_HOT_SECRET="${NEXTCLOUD_S3_HOT_SECRET:-}"
NEXTCLOUD_S3_COLD_BUCKET="${NEXTCLOUD_S3_COLD_BUCKET:-}"
NEXTCLOUD_S3_COLD_ENDPOINT="${NEXTCLOUD_S3_COLD_ENDPOINT:-}"
NEXTCLOUD_S3_COLD_ACCESS_KEY="${NEXTCLOUD_S3_COLD_ACCESS_KEY:-}"
NEXTCLOUD_S3_COLD_SECRET="${NEXTCLOUD_S3_COLD_SECRET:-}"

# Set by step_check_repo to "hot" or "cold" once a reachable repo is
# confirmed. step_summary reports it; future steps may branch on it.
USED_BUCKET=""

# === CATENA_ENVELOPE_HOOK_BEGIN ============================================
# RESERVED: the future OliveTin recovery-archive generator replaces this
# block with `step_decrypt_envelope` plus an embedded
# password-encrypted creds blob. While empty, the script falls back to
# prompting the operator/client for credentials via step_collect_inputs.
# Do not delete the markers; the generator pattern-matches on them.
# === CATENA_ENVELOPE_HOOK_END ==============================================

log() { printf '%s %s\n' "$LOG_PREFIX" "$*" >&2; }
warn() { printf '%s WARN: %s\n' "$LOG_PREFIX" "$*" >&2; }
die() { printf '%s ERROR: %s\n' "$LOG_PREFIX" "$*" >&2; exit 1; }

prompt_secret() {
    # $1 = var name, $2 = human-readable label.
    # If the env var is already set, use it. Otherwise read from stdin
    # without echoing.
    local _name="$1"
    local _label="$2"
    if [ -n "${!_name:-}" ]; then return 0; fi
    if [ ! -t 0 ]; then
        die "$_name not set and stdin is not a terminal; export it before running"
    fi
    printf '%s %s: ' "$LOG_PREFIX" "$_label" >&2
    IFS= read -rs _value
    printf '\n' >&2
    if [ -z "$_value" ]; then die "$_label cannot be empty"; fi
    eval "export $_name=\"\$_value\""
}

prompt_visible() {
    local _name="$1"
    local _label="$2"
    if [ -n "${!_name:-}" ]; then return 0; fi
    if [ ! -t 0 ]; then
        die "$_name not set and stdin is not a terminal; export it before running"
    fi
    printf '%s %s: ' "$LOG_PREFIX" "$_label" >&2
    IFS= read -r _value
    if [ -z "$_value" ]; then die "$_label cannot be empty"; fi
    eval "export $_name=\"\$_value\""
}

require_root() {
    if [ "$(id -u)" -ne 0 ]; then
        die "must be run as root (try: sudo $0)"
    fi
}

step_intro() {
    cat >&2 <<'EOF'
[restore] catena client-side recovery
[restore]
[restore] This script restores your VPS from your restic backup bucket
[restore] onto this fresh host. It does NOT modify your backup bucket
[restore] (read-only access is enough for the restore step). It WILL
[restore] write into /mnt/data, /etc, and start Docker on this host.
[restore]
[restore] Press Ctrl-C now if this is not a fresh VPS you intend to
[restore] overwrite. Otherwise, hit Enter to continue.
EOF
    if [ -t 0 ]; then read -r _ack || true; fi
}

step_collect_inputs() {
    log "Step 1/8 -- collect credentials"
    prompt_visible RESTIC_REPOSITORY "restic repo URL (e.g. s3:s3.us-east-005.backblazeb2.com/acme-vps)"
    prompt_secret  RESTIC_PASSWORD   "restic repo encryption password"
    prompt_visible AWS_ACCESS_KEY_ID "S3 access key"
    prompt_secret  AWS_SECRET_ACCESS_KEY "S3 secret key"

    if [ -z "${CLOUDFLARED_TOKEN:-}" ] && [ -t 0 ]; then
        printf '%s install cloudflared with a new token? [y/N]: ' "$LOG_PREFIX" >&2
        IFS= read -r _yn
        if [ "$_yn" = "y" ] || [ "$_yn" = "Y" ]; then
            prompt_secret CLOUDFLARED_TOKEN "Cloudflare tunnel token"
        fi
    fi

    export RESTIC_REPOSITORY RESTIC_PASSWORD AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY
}

step_prepare_host() {
    log "Step 2/8 -- prepare host (apt update + install prereqs)"
    if ! command -v apt-get >/dev/null 2>&1; then
        die "this script targets Debian/Ubuntu (apt-get not on PATH)"
    fi

    DEBIAN_FRONTEND=noninteractive apt-get update -qq
    # iptables is REQUIRED by docker's bridge driver: without it,
    # `dockerd` fails on first boot with "failed to register 'bridge'
    # driver: failed to create NAT chain DOCKER: iptables not found",
    # which then blocks the dokploy install.sh step. Debian 13 cloud
    # images don't ship iptables by default, so install it explicitly
    # before the dokploy installer attempts `docker swarm init`. (The
    # operator path's bootstrap.yml installs iptables via the docker
    # role's package list; the client recovery path has to do it
    # itself since it doesn't run through the role.)
    DEBIAN_FRONTEND=noninteractive apt-get install -y \
        --no-install-recommends \
        restic curl ca-certificates ssl-cert python3 python3-pip iptables >/dev/null

    # boto3 is needed by the embedded bucket-mirror python helper
    # (step_nextcloud_s3_repair). Install only when the NC-S3 envelope
    # is set so vanilla restore.sh runs (no Nextcloud) stay stdlib-only.
    if [ -n "$NEXTCLOUD_S3_HOT_BUCKET" ] || [ -n "$NEXTCLOUD_S3_COLD_BUCKET" ]; then
        DEBIAN_FRONTEND=noninteractive apt-get install -y \
            --no-install-recommends python3-boto3 >/dev/null \
            || pip3 install --break-system-packages boto3 >/dev/null
    fi

    mkdir -p "$DATA_DIR"
    log "  $DATA_DIR ready"
    log "  restic version: $(restic version | head -n1)"
}

step_check_repo() {
    log "Step 3/8 -- verify restic repo is reachable"
    if restic snapshots --latest 1 >/dev/null 2>&1; then
        USED_BUCKET="hot"
        log "  ok (hot) -- latest snapshot:"
        restic snapshots --latest 1 2>/dev/null | tail -n +1 | sed 's/^/[restore]   /' >&2
        return 0
    fi
    # Hot bucket unreachable. If the envelope carries cold creds, try
    # the cold mirror before giving up. Reassign the well-known restic
    # env vars so step_restore (and everything downstream) reads from
    # the cold bucket without further conditionals.
    if [ -z "$RESTIC_REPOSITORY_COLD" ] \
        || [ -z "$AWS_ACCESS_KEY_ID_COLD" ] \
        || [ -z "$AWS_SECRET_ACCESS_KEY_COLD" ]; then
        die "cannot reach restic repo. Check RESTIC_REPOSITORY, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, RESTIC_PASSWORD. (No cold creds supplied to fall back on.)"
    fi
    warn "  hot bucket unreachable; falling back to cold mirror"
    export RESTIC_REPOSITORY="$RESTIC_REPOSITORY_COLD"
    export AWS_ACCESS_KEY_ID="$AWS_ACCESS_KEY_ID_COLD"
    export AWS_SECRET_ACCESS_KEY="$AWS_SECRET_ACCESS_KEY_COLD"
    if ! restic snapshots --latest 1 >/dev/null 2>&1; then
        die "cannot reach restic repo via either hot or cold creds. Restore aborted."
    fi
    USED_BUCKET="cold"
    log "  ok (cold fallback) -- latest snapshot:"
    restic snapshots --latest 1 2>/dev/null | tail -n +1 | sed 's/^/[restore]   /' >&2
}

step_restore() {
    log "Step 4/8 -- restore latest snapshot to /"
    if [ -d "$DATA_DIR/docker/volumes" ] && [ -n "$(ls -A "$DATA_DIR/docker/volumes" 2>/dev/null)" ]; then
        warn "  $DATA_DIR/docker/volumes already populated -- skipping restic restore (re-run with a clean host to force)"
        return 0
    fi
    restic restore latest --target / 2>&1 | sed 's/^/[restore]   /' >&2
    log "  restore complete"
}

step_sanitize_apt_sources() {
    # Restored /etc carries every /etc/apt/sources.list.d/*.list the
    # source VPS had. Some of those reference signed-by= keyrings that
    # live OUTSIDE /etc (notably tailscale, which ships the upstream
    # source list pointing at /usr/share/keyrings/tailscale-archive-
    # keyring.gpg). The restic snapshot covers /etc + /var/lib/dpkg +
    # /usr/local/bin but NOT /usr/share, so on a fresh recovery target
    # the keyring is absent and the next `apt-get update` (run by
    # Dokploy's install.sh) emits "repository is not signed", which can
    # cascade into the docker install failing silently.
    #
    # Disable any sources.list.d entry whose signed-by keyring is
    # missing on this host. Renames .list -> .list.disabled-by-restore
    # so the operator can re-enable post-recovery (e.g. after
    # bootstrap.yml re-installs tailscale + its keyring). Idempotent on
    # re-run because the loop only matches *.list.
    log "Step 4.5/8 -- sanitize broken apt sources from snapshot"
    if ! command -v grep >/dev/null 2>&1; then
        warn "  grep missing, cannot sanitize; if apt-get update fails downstream, edit /etc/apt/sources.list.d/ manually"
        return 0
    fi
    # Note: capturing `shopt -p nullglob` here would BREAK the script
    # under `set -Eeuo pipefail`. shopt -p exits 1 when the option is
    # disabled (default), and set -e propagates that out of the
    # assignment. Use `shopt -q` (a query that returns 0/1 like a test)
    # to record the prior state via a simple flag instead.
    local _disabled=0
    local _had_nullglob=0
    if shopt -q nullglob; then _had_nullglob=1; fi
    shopt -s nullglob
    for src in /etc/apt/sources.list.d/*.list; do
        local _key
        # `|| true` at the pipeline tail: grep exits 1 when there is
        # no signed-by= in the file, which under `set -Eeuo pipefail`
        # would propagate out of the command substitution and abort
        # the script. Suppress so a missing signed-by leaves _key
        # empty and the next branch falls through cleanly.
        _key="$(grep -oE 'signed-by=[^] ]+' "$src" 2>/dev/null \
            | sed -E 's/^signed-by=//' \
            | head -n1 || true)"
        if [ -n "$_key" ] && [ ! -f "$_key" ]; then
            log "  disabling $src (signed-by=$_key missing on this host)"
            mv "$src" "$src.disabled-by-restore"
            _disabled=$((_disabled + 1))
        fi
    done
    if [ "$_had_nullglob" -eq 0 ]; then
        shopt -u nullglob
    fi
    if [ "$_disabled" -gt 0 ]; then
        log "  disabled $_disabled source(s); re-enable post-recovery once their keyrings are reinstalled"
    else
        log "  no broken sources found"
    fi
}

step_install_dokploy() {
    log "Step 5/8 -- install Dokploy"
    if [ "$SKIP_DOKPLOY_INSTALL" = "1" ]; then
        log "  SKIP_DOKPLOY_INSTALL=1, skipping"
        return 0
    fi
    if command -v docker >/dev/null 2>&1 && docker info 2>/dev/null | head -1 | grep -qi 'swarm'; then
        log "  docker swarm already initialized -- assuming Dokploy is present, skipping installer"
        return 0
    fi
    # The restored /var/lib/dpkg lists docker-* packages at the
    # version the source VPS had. The binaries themselves live in
    # /usr/bin /usr/lib/docker etc., which are NOT in backup_paths,
    # so /usr/bin/docker is absent on this fresh recovery target
    # while dpkg still claims docker-ce is installed (often at a
    # newer version than the dokploy installer's pinned 28.5.0).
    # The piped installer's `apt-get -y -qq install docker-ce=...`
    # then aborts with "Packages were downgraded and -y was used
    # without --allow-downgrades" because the operation looks like a
    # downgrade to apt.
    #
    # Resolve by purging the docker-related dpkg records for any
    # such package whose binary is missing on disk -- gives the
    # installer a clean slate without disturbing the much larger
    # set of base-system packages that came across cleanly. Idempotent
    # via `--force-all`; running twice is a no-op when the records
    # are already gone.
    if command -v dpkg >/dev/null 2>&1 && ! command -v docker >/dev/null 2>&1; then
        log "  /usr/bin/docker missing -- purging stale docker dpkg state"
        dpkg --purge --force-all \
            docker-ce docker-ce-cli docker-ce-rootless-extras \
            docker-buildx-plugin docker-compose-plugin docker-model-plugin \
            containerd.io 2>&1 \
            | sed 's/^/[restore]   /' >&2 || true
    fi
    curl -sSL https://dokploy.com/install.sh | sh 2>&1 | sed 's/^/[restore]   /' >&2
    log "  Dokploy installer finished"

    # Realign the dokploy postgres password with the restored database.
    # The installer mints a fresh random secret; the restored volume has
    # the previous one. Without realignment, Dokploy fails to start.
    log "  realigning dokploy postgres password"
    PG_CTR="$(docker ps --format '{{.Names}}' 2>/dev/null | grep dokploy-postgres | head -1 || true)"
    if [ -n "$PG_CTR" ]; then
        NEW_PW="$(docker exec "$PG_CTR" cat /run/secrets/postgres_password 2>/dev/null || true)"
        if [ -n "$NEW_PW" ]; then
            docker exec -u postgres "$PG_CTR" psql -U dokploy -d postgres \
                -c "ALTER USER dokploy WITH PASSWORD '$NEW_PW';" 2>&1 \
                | sed 's/^/[restore]   /' >&2 || warn "ALTER USER failed; you may need to run it manually"
        else
            warn "  could not read postgres_password secret; skipping ALTER"
        fi
    else
        warn "  dokploy-postgres container not found; if Dokploy is up via swarm, this is fine"
    fi
}

step_replay_pg_dumps() {
    log "Step 6/8 -- replay per-app Postgres dumps (where containers are up)"
    DUMP_DIR="$DATA_DIR/backup-staging/pg"
    if [ ! -d "$DUMP_DIR" ]; then
        log "  no $DUMP_DIR directory (no apps with Postgres in this stack), skipping"
        return 0
    fi
    if ! command -v docker >/dev/null 2>&1; then
        warn "  docker not on PATH; cannot replay dumps now. Re-run after Dokploy is up."
        return 0
    fi

    local _replayed=0
    for dump in "$DUMP_DIR"/*.sql.gz; do
        [ -f "$dump" ] || continue
        ctr="$(basename "$dump" | sed -E 's/-[0-9]+T[0-9]+Z\.sql\.gz$//')"
        if docker ps --format '{{.Names}}' | grep -Fxq "$ctr"; then
            log "  replaying $(basename "$dump") into $ctr"
            zcat "$dump" | docker exec -i "$ctr" psql -U postgres -v ON_ERROR_STOP=0 \
                >/dev/null 2>&1 || warn "    replay had errors; check container logs"
            _replayed=$((_replayed + 1))
        fi
    done
    log "  replayed $_replayed dump(s)"
    if [ "$_replayed" = "0" ]; then
        warn "  no per-app Postgres containers are up yet. Once Dokploy redeploys them, re-run this script and it will resume from this step."
    fi
}

_emit_bucket_mirror_py() {
    # Writes the embedded bucket-mirror Python helper to $1 (a path).
    # The body between BUCKET_MIRROR_PY_EMBED/END is parity-locked
    # to client-tools/_bucket_mirror.py via
    # test_restore_sh_embedded_bucket_mirror.py. Do NOT edit the body
    # here; edit the source-of-truth file and re-sync.
    cat > "$1" <<'BUCKET_MIRROR_PY_EMBED'
#!/usr/bin/env python3
"""Source-of-truth for the bucket-mirror copy embedded inside restore.sh.

restore.sh embeds this file's body verbatim as a bash heredoc so the
recovery script remains a single self-contained file -- the design
target for the OliveTin-driven encrypted-envelope flow described in
self-restore.md. This file is NOT shipped as a sibling to clients.
Two parity tests lock the architecture in place:

  - test_bucket_mirror_client_parity.py       (this file vs
                                               operator-tools/bucket-mirror.py
                                               at the library API + behaviour
                                               level; signatures, PUT order)
  - test_restore_sh_embedded_bucket_mirror.py (this file's bytes vs the
                                               heredoc embedded inside
                                               restore.sh; drift fails)

Why a self-contained copy and not an import of operator-tools/:
client-tools/ is what gets baked into the future encrypted recovery
script. Pulling in operator-tools/ + helpers/ would drag the operator
tree into the client envelope. The tool runs on a fresh recovery VPS
that has nothing on it except `pip install boto3` (handled by
restore.sh's step_prepare_host).

Bucket-mirror invocation by restore.sh: with --yes to actually copy,
without --yes to count source objects (the "NC-S3 hot bucket has
objects? if not, replay needed" probe).
"""
from __future__ import annotations

import argparse
import io
import os
import sys
from typing import Iterator

BATCH_PRINT_EVERY = 50  # progress line cadence


def s3_client(endpoint_url: str, access_key: str, secret_key: str):
    """Return a boto3 S3 client wired for an S3-compatible endpoint.

    Inlined from automation/helpers/s3_client.py so this file is
    self-contained on the client VPS.
    """
    import boto3
    region = os.environ.get("AWS_REGION") or "auto"
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
    )


def _iter_keys(client, bucket: str, prefix: str | None) -> Iterator[str]:
    paginator = client.get_paginator("list_objects_v2")
    kwargs = {"Bucket": bucket}
    if prefix:
        kwargs["Prefix"] = prefix
    for page in paginator.paginate(**kwargs):
        for obj in page.get("Contents") or []:
            yield obj["Key"]


def count_objects(client, bucket: str, prefix: str | None) -> int:
    return sum(1 for _ in _iter_keys(client, bucket, prefix))


def copy_all(
    src_client,
    src_bucket: str,
    dst_client,
    dst_bucket: str,
    *,
    src_prefix: str | None,
) -> int:
    """GET each source key, PUT it to the destination. Returns count
    copied. Parity-locked with operator-tools/bucket-mirror.py.copy_all
    so the client recovery path produces the same byte stream the
    bench validates.
    """
    total = 0
    for key in _iter_keys(src_client, src_bucket, src_prefix):
        get = src_client.get_object(Bucket=src_bucket, Key=key)
        body_bytes = get["Body"].read()
        dst_client.put_object(
            Bucket=dst_bucket,
            Key=key,
            Body=io.BytesIO(body_bytes),
        )
        total += 1
        if total % BATCH_PRINT_EVERY == 0:
            print(f"  copied {total}", file=sys.stderr, end="\r")
    print(file=sys.stderr)
    return total


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Cold->hot bucket copy for client self-restore.",
    )
    ap.add_argument("--src-endpoint-url", required=True)
    ap.add_argument("--src-bucket", required=True)
    ap.add_argument("--src-access-key", required=True)
    ap.add_argument("--src-secret-key", required=True)
    ap.add_argument("--src-prefix", default=None)
    ap.add_argument("--dst-endpoint-url", required=True)
    ap.add_argument("--dst-bucket", required=True)
    ap.add_argument("--dst-access-key", required=True)
    ap.add_argument("--dst-secret-key", required=True)
    ap.add_argument("--dst-bucket-name-confirm", default=None)
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args(argv)

    src = s3_client(args.src_endpoint_url, args.src_access_key, args.src_secret_key)
    dst = s3_client(args.dst_endpoint_url, args.dst_access_key, args.dst_secret_key)

    if not args.yes:
        count = count_objects(src, args.src_bucket, args.src_prefix)
        print(f"would copy {count} object(s)", file=sys.stderr)
        return 0

    if args.dst_bucket_name_confirm is None:
        print(
            "--yes requires --dst-bucket-name-confirm to match destination bucket",
            file=sys.stderr,
        )
        return 130
    if args.dst_bucket_name_confirm != args.dst_bucket:
        print(
            f"--dst-bucket-name-confirm {args.dst_bucket_name_confirm!r} does "
            f"not match --dst-bucket {args.dst_bucket!r}; aborted.",
            file=sys.stderr,
        )
        return 130

    total = copy_all(
        src, args.src_bucket,
        dst, args.dst_bucket,
        src_prefix=args.src_prefix,
    )
    print(f"copied {total} object(s)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
BUCKET_MIRROR_PY_EMBED
}

_nc_s3_count_objects() {
    # Counts objects in an NC-S3 bucket using the embedded bucket-mirror
    # in --no-yes mode. Returns the count on stdout, or "ERR" on failure.
    # $1=endpoint $2=bucket $3=key $4=secret
    local _bm
    _bm="$(mktemp /tmp/bucket-mirror.XXXXXX.py)"
    _emit_bucket_mirror_py "$_bm"
    # The probe uses the same endpoint/creds for src and dst because
    # bucket-mirror.py requires both; the dst is unused in --no-yes mode.
    local _out
    _out="$(python3 "$_bm" \
        --src-endpoint-url "$1" --src-bucket "$2" \
        --src-access-key "$3" --src-secret-key "$4" \
        --dst-endpoint-url "$1" --dst-bucket "$2" \
        --dst-access-key "$3" --dst-secret-key "$4" 2>&1 || echo ERR)"
    rm -f "$_bm"
    # Output looks like "would copy N object(s)"; extract N.
    if echo "$_out" | grep -qE '^would copy [0-9]+ object'; then
        echo "$_out" | sed -E 's/^would copy ([0-9]+) object.*/\1/'
        return 0
    fi
    echo "ERR"
}

step_nextcloud_s3_repair() {
    log "Step 7/8 -- Nextcloud S3 hot-bucket repair (if needed)"
    # Skip cleanly when the NC-S3 envelope is not provided. A non-NC
    # client recovery (or one where the operator did not opt into
    # client-side cold replay) reaches this step with empty vars and
    # exits without side effects.
    if [ -z "$NEXTCLOUD_S3_HOT_BUCKET" ] || [ -z "$NEXTCLOUD_S3_COLD_BUCKET" ]; then
        log "  no NC-S3 envelope present; skipping"
        return 0
    fi
    if [ -z "$NEXTCLOUD_S3_HOT_ENDPOINT" ] || [ -z "$NEXTCLOUD_S3_HOT_ACCESS_KEY" ] \
        || [ -z "$NEXTCLOUD_S3_HOT_SECRET" ] \
        || [ -z "$NEXTCLOUD_S3_COLD_ENDPOINT" ] \
        || [ -z "$NEXTCLOUD_S3_COLD_ACCESS_KEY" ] \
        || [ -z "$NEXTCLOUD_S3_COLD_SECRET" ]; then
        warn "  NC-S3 envelope is partial (bucket set but endpoint/key/secret missing); skipping repair"
        return 0
    fi

    # Probe hot bucket. Three outcomes:
    #   - reachable + has objects   -> nothing to do
    #   - reachable + empty         -> cold->hot replay
    #   - unreachable               -> warn, return (cannot replay into a bucket that does not exist)
    local _hot_count
    _hot_count="$(_nc_s3_count_objects \
        "$NEXTCLOUD_S3_HOT_ENDPOINT" "$NEXTCLOUD_S3_HOT_BUCKET" \
        "$NEXTCLOUD_S3_HOT_ACCESS_KEY" "$NEXTCLOUD_S3_HOT_SECRET")"
    if [ "$_hot_count" = "ERR" ]; then
        warn "  hot NC-S3 bucket unreachable; cannot replay into a missing bucket. Restore the bucket then re-run this script."
        return 0
    fi
    if [ "$_hot_count" -gt 0 ]; then
        log "  NC-S3 hot intact ($_hot_count objects), no replay needed"
        return 0
    fi

    log "  NC-S3 hot empty, replaying cold->hot"
    local _cold_count
    _cold_count="$(_nc_s3_count_objects \
        "$NEXTCLOUD_S3_COLD_ENDPOINT" "$NEXTCLOUD_S3_COLD_BUCKET" \
        "$NEXTCLOUD_S3_COLD_ACCESS_KEY" "$NEXTCLOUD_S3_COLD_SECRET")"
    if [ "$_cold_count" = "ERR" ] || [ "$_cold_count" -eq 0 ]; then
        die "NC-S3 cold bucket is unreachable or empty ($_cold_count); cannot recover Nextcloud user data."
    fi

    local _bm
    _bm="$(mktemp /tmp/bucket-mirror.XXXXXX.py)"
    _emit_bucket_mirror_py "$_bm"
    python3 "$_bm" \
        --src-endpoint-url "$NEXTCLOUD_S3_COLD_ENDPOINT" \
        --src-bucket "$NEXTCLOUD_S3_COLD_BUCKET" \
        --src-access-key "$NEXTCLOUD_S3_COLD_ACCESS_KEY" \
        --src-secret-key "$NEXTCLOUD_S3_COLD_SECRET" \
        --dst-endpoint-url "$NEXTCLOUD_S3_HOT_ENDPOINT" \
        --dst-bucket "$NEXTCLOUD_S3_HOT_BUCKET" \
        --dst-access-key "$NEXTCLOUD_S3_HOT_ACCESS_KEY" \
        --dst-secret-key "$NEXTCLOUD_S3_HOT_SECRET" \
        --dst-bucket-name-confirm "$NEXTCLOUD_S3_HOT_BUCKET" \
        --yes 2>&1 | sed 's/^/[restore]   /' >&2 \
        || { rm -f "$_bm"; die "cold->hot replay failed; check creds + bucket names"; }
    rm -f "$_bm"

    # Verify the replay landed by re-counting hot.
    local _verify_count
    _verify_count="$(_nc_s3_count_objects \
        "$NEXTCLOUD_S3_HOT_ENDPOINT" "$NEXTCLOUD_S3_HOT_BUCKET" \
        "$NEXTCLOUD_S3_HOT_ACCESS_KEY" "$NEXTCLOUD_S3_HOT_SECRET")"
    if [ "$_verify_count" = "ERR" ] || [ "$_verify_count" -lt "$_cold_count" ]; then
        die "post-replay hot count ($_verify_count) is below cold count ($_cold_count); recovery is incomplete"
    fi
    log "  replayed $_verify_count object(s) cold->hot"

    # If Nextcloud app container is up, reconcile oc_filecache so the
    # restored bucket is visible to logged-in users immediately.
    if command -v docker >/dev/null 2>&1; then
        local _nc_app
        _nc_app="$(docker ps --format '{{.Names}}' 2>/dev/null | grep -E 'nextcloud-.*-app' | head -1 || true)"
        if [ -n "$_nc_app" ]; then
            log "  running occ files:scan --all in $_nc_app"
            docker exec -u www-data "$_nc_app" php occ files:scan --all 2>&1 \
                | sed 's/^/[restore]   /' >&2 \
                || warn "  occ files:scan reported errors; users may need a refresh"
        else
            warn "  no Nextcloud app container running yet; re-run this script after Dokploy redeploys it to reconcile oc_filecache"
        fi
    fi
}

step_install_cloudflared() {
    log "Step 8/8 -- install cloudflared"
    if [ -z "${CLOUDFLARED_TOKEN:-}" ]; then
        log "  no CLOUDFLARED_TOKEN supplied, skipping (your existing tunnel may still be valid)"
        return 0
    fi
    if [ -f /etc/systemd/system/cloudflared.service ] && systemctl is-active --quiet cloudflared 2>/dev/null; then
        warn "  cloudflared.service already active; rotate manually:"
        warn "    systemctl stop cloudflared"
        warn "    cloudflared service uninstall"
        warn "    cloudflared service install '\$CLOUDFLARED_TOKEN'"
        return 0
    fi
    if ! command -v cloudflared >/dev/null 2>&1; then
        local _arch
        _arch="$(dpkg --print-architecture 2>/dev/null || echo amd64)"
        local _url="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${_arch}.deb"
        log "  downloading $_url"
        curl -fsSLo /tmp/cloudflared.deb "$_url"
        DEBIAN_FRONTEND=noninteractive dpkg -i /tmp/cloudflared.deb >/dev/null 2>&1 \
            || die "cloudflared install failed; install manually then re-run with SKIP_DOKPLOY_INSTALL=1"
        rm -f /tmp/cloudflared.deb
    fi
    cloudflared service install "$CLOUDFLARED_TOKEN" 2>&1 | sed 's/^/[restore]   /' >&2
    log "  cloudflared service installed; check status with: systemctl status cloudflared"
}

step_summary() {
    cat >&2 <<EOF
[restore]
[restore] Restore complete. Sanity checks:
[restore]
[restore]   1. systemctl status docker
[restore]   2. docker ps                    # see your apps' containers
[restore]   3. visit https://<auth-domain>  # Keycloak login
[restore]   4. visit https://<apps-domain>  # Dokploy admin
[restore]
[restore] If apps show as "stopped" in Dokploy, click "Deploy" on each.
[restore] If a per-app database is empty after the redeploy, re-run this
[restore] script -- step 6 (replay pg dumps) will pick up the now-running
[restore] containers and replay their dumps.
[restore]
[restore] If anything is wrong: contact your operator. They have the
[restore] same access this script does.
EOF
}

main() {
    require_root
    step_intro
    step_collect_inputs
    step_prepare_host
    step_check_repo
    step_restore
    step_sanitize_apt_sources
    step_install_dokploy
    step_replay_pg_dumps
    step_nextcloud_s3_repair
    step_install_cloudflared
    step_summary
}

# Run main only when invoked as a script. Sourced (e.g. from a unit
# test that wants to drive step_check_repo with a stub `restic` on
# PATH) -- no-op.
if [ "${BASH_SOURCE[0]:-$0}" = "$0" ]; then
    main "$@"
fi
