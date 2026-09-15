#!/bin/sh
# Inject the renewed mail TLS cert into the dms container and reload
# Postfix + Dovecot so it takes effect without dropping established
# connections.
#
# Installed verbatim to
# /etc/letsencrypt/renewal-hooks/deploy/mailserver-reload.sh by
# reconcile/roles/infrastructure mailserver_cert.yml.
#
# certbot fires deploy hooks after every
# successful renewal (for ALL certs that renewed in the run); this also
# runs once at converge for first issuance.
#
# Plain script -- NO Jinja -- per repo policy (executables carry no
# template markers; per-host values arrive via the env file below).
#
# No-op (exit 0) when: some OTHER cert renewed (not the mail one), the
# mail cert is absent, or the mailserver stack is not deployed on this
# host -- so it never breaks an unrelated certbot renewal run.
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

# The SERVICE is the discriminator, not the container -- the same one
# reconcile/roles/infrastructure/tasks/_mailserver_dms_locate.yml uses. "No service"
# is a genuine not-deployed and stays an instant no-op, so an unrelated
# renewal run is never delayed. "Service but no running container" is dms
# cycling, which is worth waiting out below.
#
# Asked explicitly rather than through a pipeline, because `svc=$(docker
# service ls | head -n1)` reports head's status: a docker that could not
# answer would read as an empty result, and "nothing to find" and "failed
# to look" would become the same silent exit 0.
if ! svc_out=$(docker service ls \
        --filter 'label=com.docker.stack.namespace=catena-mailserver' \
        --format '{{.Name}}' 2>&1); then
    echo "mailserver cert: could not ask docker whether the mailserver stack" \
         "is deployed: $svc_out" >&2
    exit 1
fi
[ -n "$(printf '%s\n' "$svc_out" | head -n1)" ] || exit 0

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

# Resolve and inject as ONE retried unit. dms is deliberately unstable while
# this runs: docker-mailserver polls 120s for a mailbox and shuts down without
# one, and the mailbox is created by a LATER task, so the container cycles up
# and down throughout. A name from `docker ps` is therefore good for an instant,
# and a `docker exec` against a container that has since stopped exits non-zero
# -- which under `set -e` aborted the hook and failed the converge with
#
#   Error response from daemon: container 48f7969363cc is not running
#
# Retrying the exec alone would not fix it: the replacement container carries a
# different name, so the lookup has to happen again inside every attempt.
inject_once() {
    ct=$(docker ps --filter 'label=vps.component=dms' \
        --format '{{.Names}}' | head -n1)
    [ -n "$ct" ] || return 1

    cur_hash=$(docker exec "$ct" sha256sum "$DEST/fullchain.pem" 2>/dev/null \
        | { read -r h _; echo "$h"; } || true)
    if [ -n "$live_hash" ] && [ "$live_hash" = "$cur_hash" ]; then
        echo "mailserver cert already current in $ct; no reload needed"
        return 0
    fi

    # certbot's live/*.pem are symlinks into ../../archive/<lineage>/. Plain
    # `docker cp` copies the SYMLINK verbatim, which lands as a dangling link
    # inside the container (the archive/ path does not exist there) and the
    # daemon rejects it: "invalid symlink ... -> ../../archive/...". Pass -L
    # so docker cp follows the symlink and copies the real cert bytes into
    # the mounted volume.
    docker exec "$ct" mkdir -p "$DEST" || return 1
    docker cp -L "$LIVE/fullchain.pem" "$ct:$DEST/fullchain.pem" || return 1
    docker cp -L "$LIVE/privkey.pem"   "$ct:$DEST/privkey.pem" || return 1

    # Reload both MTAs to re-read the cert. `|| true` so a transient reload
    # hiccup does not fail the certbot renewal run -- the bytes are already
    # in place; the next connection picks them up.
    docker exec "$ct" postfix reload || true
    docker exec "$ct" doveadm reload || true
    echo "mailserver cert reloaded into $ct"
    return 0
}

# 18 x 10s spans more than one full 120s account-wait plus restart, so a
# flapping dms is caught in an up window rather than missed.
attempt=1
while [ "$attempt" -le 18 ]; do
    if inject_once; then
        exit 0
    fi
    attempt=$((attempt + 1))
    sleep 10
done

echo "mailserver cert: catena-mailserver is deployed but no dms container" \
     "stayed up long enough to take the cert within 180s" >&2
exit 1
