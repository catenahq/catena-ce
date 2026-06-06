#!/bin/sh
# Installed verbatim to
# /etc/letsencrypt/renewal-hooks/deploy/mailserver-reload.sh by
# roles/infrastructure mailserver_cert.yml.
#
# Catena: inject the renewed mail TLS cert into the dms container and
# reload Postfix + Dovecot so it takes effect without dropping
# established connections. certbot fires deploy hooks after every
# successful renewal (for ALL certs that renewed in the run); this also
# runs once at converge for first issuance.
#
# Plain script -- NO Jinja -- per repo policy (executables carry no
# template markers; per-host values arrive via the env file below).
#
# No-op (exit 0) when: some OTHER cert renewed (not the mail one), the
# mail cert is absent, or no dms container is running -- so it never
# breaks an unrelated certbot renewal run.
set -eu

ENV_FILE=/etc/catena/mailserver-cert.env
[ -f "$ENV_FILE" ] || exit 0
# shellcheck disable=SC1090
. "$ENV_FILE"

: "${MAIL_CERT_LIVE_DIR:?MAIL_CERT_LIVE_DIR missing from $ENV_FILE}"

# certbot sets RENEWED_LINEAGE on a renewal run. Act only when the mail
# lineage is the one that renewed. When invoked manually (converge, no
# RENEWED_LINEAGE in env), act unconditionally on the mail cert.
if [ -n "${RENEWED_LINEAGE:-}" ] && [ "$RENEWED_LINEAGE" != "$MAIL_CERT_LIVE_DIR" ]; then
    exit 0
fi

LIVE="$MAIL_CERT_LIVE_DIR"
DEST="/tmp/docker-mailserver/custom-certs"
[ -f "$LIVE/fullchain.pem" ] || exit 0

ct=$(docker ps --filter 'label=com.docker.compose.service=dms' \
    --format '{{.Names}}' | head -n1)
[ -n "$ct" ] || exit 0

# Idempotency: if the cert already inside the container matches the live
# cert, do nothing (no copy, no reload) so a converge that did not rotate
# the cert is a true no-op. Without this the converge-time invocation
# reloads on every run and the task reports `changed` forever, failing
# the bench idempotency stage. sha256sum follows the live/*.pem symlinks,
# hashing the real bytes; the in-container copy was written with cp -L so
# it is real bytes too. On first issuance the container copy is absent
# (empty hash) so we fall through and inject. On renewal the bytes differ
# so we inject + reload.
live_hash=$(sha256sum "$LIVE/fullchain.pem" 2>/dev/null | { read -r h _; echo "$h"; })
cur_hash=$(docker exec "$ct" sha256sum "$DEST/fullchain.pem" 2>/dev/null \
    | { read -r h _; echo "$h"; } || true)
if [ -n "$live_hash" ] && [ "$live_hash" = "$cur_hash" ]; then
    echo "mailserver cert already current in $ct; no reload needed"
    exit 0
fi

# certbot's live/*.pem are symlinks into ../../archive/<lineage>/. Plain
# `docker cp` copies the SYMLINK verbatim, which lands as a dangling link
# inside the container (the archive/ path does not exist there) and the
# daemon rejects it: "invalid symlink ... -> ../../archive/...". Pass -L
# so docker cp follows the symlink and copies the real cert bytes into
# the mounted volume.
docker exec "$ct" mkdir -p "$DEST"
docker cp -L "$LIVE/fullchain.pem" "$ct:$DEST/fullchain.pem"
docker cp -L "$LIVE/privkey.pem"   "$ct:$DEST/privkey.pem"

# Reload both MTAs to re-read the cert. `|| true` so a transient reload
# hiccup does not fail the certbot renewal run -- the bytes are already
# in place; the next connection picks them up.
docker exec "$ct" postfix reload || true
docker exec "$ct" doveadm reload || true
echo "mailserver cert reloaded into $ct"
