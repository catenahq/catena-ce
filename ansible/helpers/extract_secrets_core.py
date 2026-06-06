"""Core logic for reconstructing a candidate vault.yml from a live VPS.

Used when the operator has lost the vault password but the host is still
running. Shared between:

  - operator-tools/extract_secrets_from_host.py (runs on the operator's
    laptop, calls commands on the host over SSH)
  - vps-scripts/generate-recovery-archive.py (runs on the host itself,
    invoked by the "Generate recovery archive (encrypted)" catena-admin
    Recovery-tab button; vault.recovered.yml inside the produced .zip
    is this module's emit())

Both share the LOCATIONS table below. The host-side script vendors a
copy via roles/infrastructure/tasks/recovery_archive.yml; a unit test
asserts byte-equality of the two so they can't drift.

Contract:
  extract(run_cmd) takes a callable that runs a shell string on the
  target (local or remote) and returns (rc, stdout, stderr). Callers
  inject whatever plumbing they need (subprocess, paramiko, ssh -o ...).
  Tests stub it with a dict of canned responses.

Not recoverable from the host (must be re-minted in a provider's admin
console) are marked kind="provider-only" and surface as commented
placeholders in the YAML output, with a human-readable hint pointing at
the right admin page.
"""
from __future__ import annotations

import fnmatch
from collections import namedtuple
from dataclasses import dataclass, field
from typing import Callable

CommandResult = namedtuple("CommandResult", ["rc", "stdout", "stderr"])
RunCmd = Callable[[str], CommandResult]


# Map from vault_* key to where it lives on a live host.
#
#   kind=host-file      path=<file>         [env_key=<KEY>]
#     Read plaintext from <file>. If env_key is set, parse the file as
#     KEY=VAL lines and return VAL for that key (dotenv-style).
#
#   kind=container-exec container=<pat>     path=<file-in-container>
#     Resolve <pat> to a container name via `docker ps`, then
#     `docker exec <name> cat <file-in-container>`.
#
#   kind=container-env  container=<pat>     env=<VAR>
#     Resolve <pat>, then `docker inspect` the container and pull <VAR>
#     out of .Config.Env.
#
#   kind=provider-only  hint=<text>
#     Cannot be reconstructed from the host. The extract() output emits
#     a commented placeholder with the hint, so the operator knows where
#     to re-mint it.
#
# <pat> is an fnmatch glob matched against `docker ps --format '{{.Names}}'`.
# Dokploy names containers "<app>-<hash>-<service>-<idx>" so a pattern like
# "<service>-*-<role>-1" pins to a Dokploy-managed container across re-deploys.
# optional=True: if no container matches, silently omit the key instead of
# flagging it as RECOVERY-FAILED (Nextcloud-S3 + SMTP-disabled paths).
LOCATIONS: dict[str, dict] = {
    # ── Backup / restic ──────────────────────────────────────────────
    "vault_backup_restic_password": {
        "kind": "host-file",
        # roles/backup writes the password to /etc/catena/restic.pass
        # (see roles/backup/defaults/main.yml :: backup_password_file).
        # An earlier version of this table had /etc/catena/backup/
        # restic.pass, which never existed -- confirmed April 2026 on a
        # live host.
        "path": "/etc/catena/restic.pass",
    },
    "vault_backup_s3_access_key": {
        "kind": "host-file",
        "path": "/etc/catena/backup.env",
        "env_key": "AWS_ACCESS_KEY_ID",
    },
    "vault_backup_s3_secret_key": {
        "kind": "host-file",
        "path": "/etc/catena/backup.env",
        "env_key": "AWS_SECRET_ACCESS_KEY",
    },
    "vault_backup_worm_access_key": {
        "kind": "host-file",
        "path": "/etc/catena/backup.env",
        "env_key": "BACKUP_WORM_ACCESS_KEY_ID",
        "optional": True,  # WORM mirror is opt-in; daily backups don't need it
    },
    "vault_backup_worm_secret_key": {
        "kind": "host-file",
        "path": "/etc/catena/backup.env",
        "env_key": "BACKUP_WORM_SECRET_ACCESS_KEY",
        "optional": True,
    },
    "vault_nextcloud_worm_access_key": {
        "kind": "host-file",
        "path": "/etc/catena/backup.env",
        "env_key": "NEXTCLOUD_WORM_ACCESS_KEY_ID",
        "optional": True,  # Nextcloud cold-tier mirror is opt-in
    },
    "vault_nextcloud_worm_secret_key": {
        "kind": "host-file",
        "path": "/etc/catena/backup.env",
        "env_key": "NEXTCLOUD_WORM_SECRET_ACCESS_KEY",
        "optional": True,
    },
    # ── Dokploy ──────────────────────────────────────────────────────
    "vault_dokploy_postgres_password": {
        "kind": "container-exec",
        # Dokploy runs postgres as a Docker Swarm service, so the live
        # container name is `dokploy-postgres.<replica>.<taskid>` (e.g.
        # `dokploy-postgres.1.0dlk3znfyhktcja6gz4hh03c8`). A bare
        # `dokploy-postgres` exact-match never resolved. The `*` also
        # covers a hypothetical compose-style `dokploy-postgres-1` name
        # if Dokploy ever moves off swarm. fnmatch `*` doesn't cross
        # slashes but container names don't contain slashes, so matching
        # is unambiguous.
        "container": "dokploy-postgres*",
        "path": "/run/secrets/postgres_password",
    },
    # ── SSO (Keycloak Phase Two) ─────────────────────────────────────
    "vault_keycloak_db_password": {
        "kind": "container-env",
        "container": "keycloak-*-server-*",
        "env": "KC_DB_PASSWORD",
    },
    "vault_admin_password": {
        "kind": "container-env",
        "container": "keycloak-*-server-*",
        "env": "KC_BOOTSTRAP_ADMIN_PASSWORD",
        # Initial-admin envs are read ONLY on first boot. After that
        # the running container can have empty/rotated env values
        # while the original admin password lives in the realm DB.
        # Best-effort extraction; the vault is the source of truth.
        "optional": True,
    },
    "vault_oauth2_proxy_client_secret": {
        "kind": "container-env",
        # The static client lives in keycloak-config-cli's realm config,
        # so the running oauth2-proxy container is the cleanest source of
        # truth for the in-use secret. Either staff or admin instance
        # works -- they share the secret.
        "container": "oauth2-proxy-*-staff-*",
        "env": "OAUTH2_PROXY_CLIENT_SECRET",
        "optional": True,
    },
    "vault_oauth2_proxy_cookie_secret": {
        "kind": "container-env",
        "container": "oauth2-proxy-*-staff-*",
        "env": "OAUTH2_PROXY_COOKIE_SECRET",
        "optional": True,
    },
    # dashboard-sync's Keycloak service-account secret (manage-clients).
    # roles/infrastructure renders it into /etc/catena/dashboard-sync.env
    # as DASHBOARD_SYNC_CLIENT_SECRET so the host-side dashboard-sync
    # script can mint client-credentials tokens. Plain host file, so it
    # recovers directly. optional=True: the env line uses `default('')`,
    # so an operator who has not added the key yet leaves it empty (the
    # redirect-URI sync is skipped, not fatal).
    "vault_dashboard_sync_client_secret": {
        "kind": "host-file",
        "path": "/etc/catena/dashboard-sync.env",
        "env_key": "DASHBOARD_SYNC_CLIENT_SECRET",
        "optional": True,
    },
    # Nextcloud's native-OIDC client secret (EP3). Dokploy compose
    # passes it as the NEXTCLOUD_OIDC_CLIENT_SECRET env var, which
    # the post-deploy `occ user_oidc:provider` task in nextcloud_oidc.yml
    # consumes. Optional because Nextcloud is a Dokploy template the
    # operator deploys at-will -- no container exists on every host.
    "vault_nextcloud_oidc_client_secret": {
        "kind": "container-env",
        "container": "nextcloud-*-app-*",
        "env": "NEXTCLOUD_OIDC_CLIENT_SECRET",
        "optional": True,
    },
    # ── Healthchecks ─────────────────────────────────────────────────
    "vault_healthchecks_secret_key": {
        "kind": "container-env",
        "container": "healthchecks-*-app-*",
        "env": "SECRET_KEY",
    },
    "vault_healthchecks_superuser_password": {
        "kind": "container-env",
        "container": "healthchecks-*-app-*",
        "env": "SUPERUSER_PASSWORD",
    },
    "vault_healthchecks_ping_key": {
        "kind": "container-env",
        "container": "healthchecks-*-app-*",
        "env": "PING_KEY",
    },
    "vault_healthchecks_api_key_readonly": {
        "kind": "host-file",
        "path": "/etc/catena/gatus-sync.env",
        "env_key": "HEALTHCHECKS_API_KEY",
    },
    "vault_healthchecks_api_key_readwrite": {
        "kind": "host-file",
        "path": "/etc/catena/gatus-sync.env",
        "env_key": "HEALTHCHECKS_API_KEY_RW",
    },
    # ── SMTP ─────────────────────────────────────────────────────────
    # vault_smtp_password is set in the Keycloak realm's smtpServer
    # block (rendered by realm-vps.yaml.j2 + applied via keycloak-config-
    # cli) -- it does NOT appear as a plain env var on the keycloak-server
    # container. The vault is the source of truth; recovery from a
    # running host requires reading the realm config via Admin REST.
    # Auto-extraction support deferred until BACKLOG_TECHNICAL.md EP3 ships.
    # ── Nextcloud-S3 (client-specific, may not be present) ───────────
    "vault_nextcloud_s3_access_key": {
        "kind": "container-env",
        "container": "nextcloud-*-app-*",
        "env": "OBJECTSTORE_S3_KEY",
        "optional": True,
    },
    "vault_nextcloud_s3_secret_key": {
        "kind": "container-env",
        "container": "nextcloud-*-app-*",
        "env": "OBJECTSTORE_S3_SECRET",
        "optional": True,
    },
    # ── Chat-video stacks (coturn + bundled Jitsi sidecars + Talk HPB) ─
    # The shared coturn role renders the static-auth-secret to
    # /etc/coturn/turnserver.conf as a `static-auth-secret=<value>` line.
    # Both Rocket.Chat's bundled Jitsi (JVB_TURN_SECRET) and Nextcloud
    # Talk's aio-talk container (TURN_STATIC_AUTH_SECRET) consume the
    # same secret, but coturn itself is the source of truth -- recover
    # from the conf file and feed both consumers. The conf file is
    # dotenv-shaped (KEY=VALUE per line) so env_key parsing works.
    "vault_turn_static_auth_secret": {
        "kind": "host-file",
        "path": "/etc/coturn/turnserver.conf",
        "env_key": "static-auth-secret",
    },
    # Bundled Jitsi sidecars ship as separate services in the rocket-chat
    # compose. The whole JITSI block can be commented out (chat-only
    # deployments) so all four are optional. Container names follow
    # Dokploy "<app>-<hash>-<service>-<idx>".
    "vault_jitsi_jicofo_auth_password": {
        "kind": "container-env",
        "container": "rocketchat-*-jicofo-*",
        "env": "JICOFO_AUTH_PASSWORD",
        "optional": True,
    },
    "vault_jitsi_jicofo_component_secret": {
        "kind": "container-env",
        "container": "rocketchat-*-jicofo-*",
        "env": "JICOFO_COMPONENT_SECRET",
        "optional": True,
    },
    "vault_jitsi_jvb_auth_password": {
        "kind": "container-env",
        "container": "rocketchat-*-jvb-*",
        "env": "JVB_AUTH_PASSWORD",
        "optional": True,
    },
    # vault_jitsi_prosody_password is auto-minted in seed.py for future
    # use by the prosody sidecar but the current rocketchat.compose.yml
    # does not yet inject it (prosody runs with internal-auth defaults).
    # Marked optional with a prosody-container target so when the compose
    # is extended to forward PROSODY_PASSWORD the recovery path works
    # without a LOCATIONS bump.
    "vault_jitsi_prosody_password": {
        "kind": "container-env",
        "container": "rocketchat-*-prosody-*",
        "env": "PROSODY_PASSWORD",
        "optional": True,
    },
    # ── Element / Matrix template ───────────────────────────────────
    # Element / Matrix stack: synapse homeserver + bundled Jitsi + jigasi
    # (SIP gateway). Vault key family is *_element_* so it does not
    # collide with the rocketchat-bundled Jitsi above when both
    # templates coexist on the same host.
    "vault_element_oidc_client_secret": {
        "kind": "container-env",
        "container": "element-*-synapse-*",
        "env": "OIDC_CLIENT_SECRET",
        "optional": True,
    },
    "vault_element_jitsi_jicofo_auth_password": {
        "kind": "container-env",
        "container": "element-*-jicofo-*",
        "env": "JICOFO_AUTH_PASSWORD",
        "optional": True,
    },
    "vault_element_jitsi_jicofo_component_secret": {
        "kind": "container-env",
        "container": "element-*-jicofo-*",
        "env": "JICOFO_COMPONENT_SECRET",
        "optional": True,
    },
    "vault_element_jitsi_jvb_auth_password": {
        "kind": "container-env",
        "container": "element-*-jvb-*",
        "env": "JVB_AUTH_PASSWORD",
        "optional": True,
    },
    "vault_element_jigasi_xmpp_password": {
        "kind": "container-env",
        "container": "element-*-jigasi-*",
        "env": "JIGASI_XMPP_PASSWORD",
        "optional": True,
    },
    # Nextcloud Talk HPB (aio-talk container in the nextcloud compose).
    # Compose-level vars SIGNALING_SECRET + TALK_INTERNAL_SECRET get
    # forwarded into the talk-hpb service environment.
    "vault_nextcloud_talk_signaling_secret": {
        "kind": "container-env",
        "container": "nextcloud-*-talk-hpb-*",
        "env": "SIGNALING_SECRET",
        "optional": True,
    },
    "vault_nextcloud_talk_internal_secret": {
        "kind": "container-env",
        "container": "nextcloud-*-talk-hpb-*",
        "env": "TALK_INTERNAL_SECRET",
        "optional": True,
    },
    # ── Self-hosted mailserver (opt-in `mailserver` template) ────────
    # The outbound smarthost password is passed to the docker-mailserver
    # `dms` service as RELAY_PASSWORD (mailserver.compose.yml), re-injected
    # from the vault on every converge. Optional: the template is opt-in,
    # so no dms container exists on a tenant without mail.
    "vault_mailserver_relay_password": {
        "kind": "container-env",
        "container": "mailserver-*-dms-*",
        "env": "RELAY_PASSWORD",
        "optional": True,
    },
    # The free Spamhaus DQS key is not stored as a plain value anywhere on
    # the host -- it is interpolated into the rspamd RBL zone names
    # (`<key>.zen.dq.spamhaus.net`, ...) by mailserver_filtering.yml, which
    # docker-cp's rbl.conf into the dms container's rspamd/local.d/. Pull
    # it back out of the first DQS zone with a capture group. Optional:
    # rbl.conf is only rendered when the key is set, so an empty key leaves
    # no file (cat rc!=0 -> omitted) and a mail-less tenant has no dms.
    "vault_mailserver_spamhaus_dqs_key": {
        "kind": "container-exec",
        "container": "mailserver-*-dms-*",
        "path": "/tmp/docker-mailserver/rspamd/local.d/rbl.conf",
        "regex": r'rbl = "([^".]+)\.zen\.dq\.spamhaus\.net"',
        "optional": True,
    },
    # ── Provider-only (can't come from the host) ─────────────────────
    "vault_tailscale_oauth_client_id": {
        "kind": "provider-only",
        "hint": "Tailscale admin -> Settings -> Trust Credentials -> Generate OAuth client (scopes: Auth Keys Write, tags: tag:vps)",
    },
    "vault_tailscale_oauth_client_secret": {
        "kind": "provider-only",
        "hint": "Paired with vault_tailscale_oauth_client_id; minted in the same step",
    },
    "vault_cloudflare_api_token": {
        "kind": "provider-only",
        "hint": "https://dash.cloudflare.com/profile/api-tokens (scopes: Account > Cloudflare Tunnel > Edit, Zone > DNS > Edit)",
    },
    "vault_dokploy_api_key": {
        "kind": "provider-only",
        # Dokploy runs on the host but its API keys are managed by the
        # better-auth `apikey` plugin, which stores only an HMAC of the
        # key -- the plaintext is shown once at creation and then
        # discarded. Verified April 2026 on dev1: the `apikey.key`
        # column's first bytes do not match `apikey.start` (the 6-char
        # public prefix shown in the UI), confirming the stored value
        # is a hash, not the plaintext. So this key is provider-only
        # not by convention but because the plaintext is cryptographically
        # unrecoverable from the DB -- re-minting in the UI is the only
        # option, same as Tailscale/Cloudflare.
        "hint": "Dokploy UI -> Settings -> Profile -> API Keys -> generate one",
    },
}


@dataclass
class ExtractResult:
    """Outcome of walking the LOCATIONS table on a live host."""
    values: dict[str, str] = field(default_factory=dict)
    """Real recovered values (and provider-only sentinel strings)."""
    failures: dict[str, str] = field(default_factory=dict)
    """Keys we tried to recover but couldn't, with reason. Emitted as
    RECOVERY-FAILED markers so the operator knows to fill them in."""
    omitted: list[str] = field(default_factory=list)
    """Optional keys whose source was absent (Nextcloud not deployed,
    SMTP disabled). Emitted as comments at the top of the YAML."""
    provider_only: dict[str, str] = field(default_factory=dict)
    """Keys that are never on the host; values are the remint hint."""


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------
def extract(run_cmd: RunCmd, locations: dict[str, dict] | None = None) -> ExtractResult:
    """Walk locations, use run_cmd to probe the host, assemble result.

    run_cmd must accept a single shell string and return a CommandResult.
    Non-zero rc is treated as failure; the stderr is used as the reason
    for the RECOVERY-FAILED marker.
    """
    loc_map = locations if locations is not None else LOCATIONS
    result = ExtractResult()

    # Cache the `docker ps` output once per extract() call and match each
    # pattern against it. That's one shell invocation no matter how many
    # container-backed keys we probe.
    running_names: list[str] | None = None

    def _ensure_running_names() -> list[str] | None:
        nonlocal running_names
        if running_names is not None:
            return running_names
        probe = run_cmd("docker ps --format '{{.Names}}'")
        if probe.rc != 0:
            return None
        running_names = [ln.strip() for ln in probe.stdout.splitlines() if ln.strip()]
        return running_names

    def _resolve(pattern: str) -> str | None:
        names = _ensure_running_names()
        if names is None:
            return None
        for name in names:
            if fnmatch.fnmatchcase(name, pattern):
                return name
        return None

    for key, spec in loc_map.items():
        kind = spec["kind"]

        if kind == "provider-only":
            result.provider_only[key] = spec["hint"]
            continue

        if kind == "host-file":
            path = spec["path"]
            env_key = spec.get("env_key")
            res = run_cmd(f"cat {_sh_quote(path)}")
            if res.rc != 0:
                _record_failure(spec, result, key, f"cat {path}: {res.stderr.strip() or 'rc=' + str(res.rc)}")
                continue
            if env_key:
                value = _parse_env(res.stdout, env_key)
                if value is None:
                    _record_failure(spec, result, key, f"{env_key} not found in {path}")
                    continue
            else:
                value = res.stdout.strip()
            if not value:
                _record_failure(spec, result, key, f"{path} was empty")
                continue
            result.values[key] = value
            continue

        if kind in ("container-exec", "container-env"):
            pattern = spec["container"]
            name = _resolve(pattern)
            if name is None:
                _record_failure(spec, result, key, f"no running container matching '{pattern}'")
                continue
            if kind == "container-exec":
                path = spec["path"]
                res = run_cmd(f"docker exec {_sh_quote(name)} cat {_sh_quote(path)}")
                if res.rc != 0:
                    _record_failure(spec, result, key, f"docker exec {name} cat {path}: {res.stderr.strip() or 'rc=' + str(res.rc)}")
                    continue
                # Optional `regex` parses the file with a single capture
                # group (e.g. YAML / JSON content where the secret is one
                # field among many). Without `regex`, the whole file is
                # the value (mirrors the prior behaviour for plain-text
                # secret files like restic.pass).
                pattern_re = spec.get("regex")
                if pattern_re:
                    import re as _re
                    m = _re.search(pattern_re, res.stdout, _re.MULTILINE)
                    if m is None:
                        _record_failure(spec, result, key, f"regex {pattern_re!r} did not match in {path}")
                        continue
                    value = m.group(1) if m.groups() else m.group(0)
                else:
                    value = res.stdout.strip()
            else:  # container-env
                env_var = spec["env"]
                res = run_cmd(
                    f"docker inspect {_sh_quote(name)} "
                    "--format '{{range .Config.Env}}{{println .}}{{end}}'"
                )
                if res.rc != 0:
                    _record_failure(spec, result, key, f"docker inspect {name}: {res.stderr.strip() or 'rc=' + str(res.rc)}")
                    continue
                value = _parse_env(res.stdout, env_var)
                if value is None:
                    _record_failure(spec, result, key, f"{env_var} not set in {name} env")
                    continue
            if not value:
                # Optional keys (e.g. SMTP when disabled) legitimately end up
                # here -- treat an empty value as "not present."
                if spec.get("optional"):
                    result.omitted.append(key)
                else:
                    _record_failure(spec, result, key, f"value empty in {name}")
                continue
            result.values[key] = value
            continue

        raise ValueError(f"unknown kind {kind!r} for {key}")

    return result


def _record_failure(spec: dict, result: ExtractResult, key: str, reason: str) -> None:
    if spec.get("optional"):
        result.omitted.append(key)
    else:
        result.failures[key] = reason


def _sh_quote(s: str) -> str:
    # Good enough for our whitelisted paths and docker-ps-discovered names.
    return "'" + s.replace("'", "'\\''") + "'"


def _parse_env(blob: str, key: str) -> str | None:
    """Find KEY=VALUE in a multi-line dotenv/docker-inspect blob and
    return VALUE. Honours double/single-quoted values; strips one layer
    of matching quotes. Returns None if KEY is not present."""
    for raw in blob.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() != key:
            continue
        v = v.strip()
        # Strip surrounding quotes (one pair only).
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        return v
    return None


# ---------------------------------------------------------------------------
# YAML emission
# ---------------------------------------------------------------------------
def to_yaml(result: ExtractResult) -> str:
    """Render a candidate vault.yml from an ExtractResult.

    Shape:
      - Top-of-file comment banner explaining provenance.
      - Real values as simple quoted assignments.
      - provider_only entries commented out with a one-line hint.
      - failures rendered as RECOVERY-FAILED marker values (so that the
        operator notices when they view the file with `less`).
      - omitted keys listed in a trailing comment block.
    """
    lines: list[str] = []
    lines.append("---")
    lines.append("# Recovered from a live host by extract_secrets_core.")
    lines.append("# Not a substitute for the real vault backup, but enough to")
    lines.append("# boot an operator back into the system when the vault")
    lines.append("# password has been lost and the server is still running.")
    lines.append("#")
    lines.append("# Provider-only keys (Tailscale, Cloudflare, Dokploy API)")
    lines.append("# can never come from the host -- they are emitted as")
    lines.append("# commented placeholders below; re-mint them in the")
    lines.append("# respective admin consoles and uncomment.")
    lines.append("")

    # Real values + failures, in LOCATIONS order for stable diffs.
    for key in LOCATIONS:
        if key in result.values:
            lines.append(f'{key}: "{_yaml_escape(result.values[key])}"')
        elif key in result.failures:
            lines.append(
                f'{key}: "<RECOVERY-FAILED: {_yaml_escape(result.failures[key])}>"'
            )
        elif key in result.provider_only:
            lines.append(f"# {key}: REPLACE  # {result.provider_only[key]}")
        # omitted keys are not emitted; they get a note at the bottom.

    if result.omitted:
        lines.append("")
        lines.append("# Omitted (source absent on this host):")
        for key in result.omitted:
            lines.append(f"#   - {key}")

    lines.append("")
    return "\n".join(lines)


def _yaml_escape(s: str) -> str:
    # Our values go inside double quotes; escape backslashes and double quotes.
    return s.replace("\\", "\\\\").replace('"', '\\"')
