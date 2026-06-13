#!/bin/bash
# catena Keycloak realm export (R22).
#
# Plain bash under vps-scripts/. Per-host config arrives via
# /etc/catena/keycloak-export.env (rendered by
# roles/keycloak/tasks/realm_export.yml) which carries both the
# master-realm admin credentials AND the per-host paths/ports/names
# needed to find the running Keycloak server container.
#
# Output: ${STORAGE_MOUNT_POINT}/backup-staging/keycloak/<realm>.json
# Restic picks it up on the next nightly backup snapshot.

set -u
set -o pipefail

KC_EXPORT_CREDS_FILE="/etc/catena/keycloak-export.env"
if [ ! -r "$KC_EXPORT_CREDS_FILE" ]; then
    echo "[realm-export] $KC_EXPORT_CREDS_FILE missing or unreadable; skipping (next converge will rewrite it)." >&2
    exit 0
fi
# shellcheck disable=SC1090
. "$KC_EXPORT_CREDS_FILE"

: "${KC_ADMIN_USER:?KC_ADMIN_USER not set in $KC_EXPORT_CREDS_FILE}"
: "${KC_ADMIN_PASSWORD:?KC_ADMIN_PASSWORD not set in $KC_EXPORT_CREDS_FILE}"
: "${STORAGE_MOUNT_POINT:?STORAGE_MOUNT_POINT not set in $KC_EXPORT_CREDS_FILE}"
: "${KEYCLOAK_REALM:?KEYCLOAK_REALM not set in $KC_EXPORT_CREDS_FILE}"
: "${KEYCLOAK_INTERNAL_PORT:?KEYCLOAK_INTERNAL_PORT not set in $KC_EXPORT_CREDS_FILE}"
: "${KEYCLOAK_COMPOSE_NAME:?KEYCLOAK_COMPOSE_NAME not set in $KC_EXPORT_CREDS_FILE}"
: "${KEYCLOAK_SERVER_SERVICE:?KEYCLOAK_SERVER_SERVICE not set in $KC_EXPORT_CREDS_FILE}"
: "${KEYCLOAK_MANAGEMENT_PORT:?KEYCLOAK_MANAGEMENT_PORT not set in $KC_EXPORT_CREDS_FILE}"
: "${KEYCLOAK_HEALTH_PATH:?KEYCLOAK_HEALTH_PATH not set in $KC_EXPORT_CREDS_FILE}"

OUT_DIR="${STORAGE_MOUNT_POINT}/backup-staging/keycloak"

mkdir -p "$OUT_DIR"
chmod 0750 "$OUT_DIR"

# Resolve the running Keycloak server container by Dokploy naming
# convention. <compose>-<6-char-hash>-<svc>-<idx>; the hash rotates
# on recreate so we cannot pin it. Match prefix + suffix + first.
CT=$(docker ps --format '{{.Names}}' \
    | grep -E "^${KEYCLOAK_COMPOSE_NAME}-[a-z0-9]+-${KEYCLOAK_SERVER_SERVICE}-[0-9]+$" \
    | head -n1 || true)
if [ -z "$CT" ]; then
    echo "[realm-export] no running Keycloak server container; skipping." >&2
    exit 0
fi

# Probe /health/ready before issuing admin calls. Phase Two image
# ships without curl/wget; use bash + /dev/tcp like validate.yml.
if ! docker exec "$CT" bash -c "exec 3<>/dev/tcp/127.0.0.1/${KEYCLOAK_MANAGEMENT_PORT} && printf 'GET ${KEYCLOAK_HEALTH_PATH} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n' >&3 && head -1 <&3 | grep -q '200'"; then
    echo "[realm-export] /health/ready not 200; skipping." >&2
    exit 0
fi

# Login as master-realm admin.
docker exec "$CT" /opt/keycloak/bin/kcadm.sh config credentials \
    --server "http://localhost:${KEYCLOAK_INTERNAL_PORT}" \
    --realm master \
    --user "$KC_ADMIN_USER" \
    --password "$KC_ADMIN_PASSWORD" >/dev/null

# Dump realm + clients to backup-staging.
TS=$(date -u +%Y%m%dT%H%M%SZ)
REALM_OUT="$OUT_DIR/${KEYCLOAK_REALM}.json"
CLIENTS_OUT="$OUT_DIR/${KEYCLOAK_REALM}.clients.json"

docker exec "$CT" /opt/keycloak/bin/kcadm.sh get "realms/${KEYCLOAK_REALM}" \
    > "${REALM_OUT}.tmp"
docker exec "$CT" /opt/keycloak/bin/kcadm.sh get clients -r "${KEYCLOAK_REALM}" \
    > "${CLIENTS_OUT}.tmp"

mv "${REALM_OUT}.tmp" "${REALM_OUT}"
mv "${CLIENTS_OUT}.tmp" "${CLIENTS_OUT}"
chmod 0640 "${REALM_OUT}" "${CLIENTS_OUT}"

echo "[realm-export] wrote ${REALM_OUT} + ${CLIENTS_OUT} (ts=${TS})"
