# roles/coturn

Shared TURN/STUN server. Used by both chat-video stacks:

- **Nextcloud Talk + HPB** -- Talk reaches `turn.<base>:5349` for
  restrictive-network media relay. Configured by
  `vps-scripts/nextcloud-talk-hpb-wire.sh` via `occ talk:turn:add`.
- **Rocket.Chat's bundled Jitsi** -- jitsi-videobridge is configured
  with `JVB_TURN_HOST=turn.<base>` / `JVB_TURN_PORT=5349` /
  `JVB_TURN_SECRET={{ vault_turn_static_auth_secret }}` for the same
  relay path.

One coturn deployment serves both. Auth is `static-auth-secret`
based -- each stack mints its own ephemeral HMAC-SHA1 credentials
per call. Coturn does not maintain a per-user database.

## What this role does

1. Issues + renews a TLS cert for `turn.<base>` via Let's Encrypt
   DNS-01 (Cloudflare API). HTTP-01 is unavailable because tcp/80 is
   owned by cloudflared. Renewal handled by certbot's stock systemd
   timer; a deploy hook SIGHUPs the running coturn container so
   cert hot-reloads land without dropping calls.
2. Renders `turnserver.conf` from the role's Jinja template,
   parameterized by inventory (public IP, vault secret, hostname,
   relay port range).
3. Deploys coturn as a Docker Swarm service in `mode=global` with
   `--network=host` so the container sees the VPS public IP directly
   (required for ICE-candidate advertisement). Mirrors the
   cloudflared swarm-service pattern.

## Architecture rule revision

This role is the FIRST deliberate hole in the pre-2026-05 "all public
traffic via Cloudflare Tunnel" rule. Cloudflare Tunnel is TCP/HTTP
only; the chat-video media plane is fundamentally UDP. The rule has
been narrowed to "all TCP/HTTP public traffic via Cloudflare Tunnel;
UDP media plane direct on VPS public IP via shared coturn at
`turn.<base>` plus per-stack media ports". See
`docs/operator/hardening.md` and the architecture-doc memory entry.

## UDP exposure profile

| Port | Proto | Owner | Purpose |
|------|-------|-------|---------|
| 3478 | UDP | coturn | STUN + plain TURN |
| 5349 | TCP+UDP | coturn | TURN/TLS (restrictive-network fallback) |
| 50000-50100 | UDP | coturn | Coturn relay range (ephemeral) |
| 49160-49200 | UDP | NC Talk Janus | Talk media (when NC HPB block live) |
| 10000 | UDP | RC Jitsi JVB | Jitsi media (when RC deployed) |

## DR / portability

- Cert: regenerated automatically via certbot on the new host (state
  in `/etc/letsencrypt/`; restic-included by default).
- Static auth secret: lives in the vault (`vault_turn_static_auth_secret`),
  same DR path as every other shared secret.
- Compose / swarm spec: re-rendered on converge from this role.

## Hardening posture

The role enforces the following controls in
`templates/turnserver.conf.j2`. Each control addresses a specific
threat against a co-located TURN server reachable on the public IP.
Source list cross-referenced against Enable Security's 2026 coturn
hardening guide and the EnableSecurity/coturn-secure-config
"recommended" profile.

| Control | Threat addressed |
|---------|------------------|
| `denied-peer-ip` for every IPv4 + IPv6 special-purpose range | SSRF / pivot from an authenticated TURN client into RFC1918, loopback, cloud metadata, link-local. Without this, any chat app holding the shared secret can ask coturn to relay UDP to `127.0.0.1:5432`. |
| `denied-peer-ip=::ffff:0.0.0.0-::ffff:255.255.255.255` | CVE-2026-27624 defense in depth -- IPv4-mapped IPv6 bypass of the IPv4 denies. Image is on 4.10.0 (patched); guard protects against a future downgrade. |
| `no-loopback-peers` + `no-multicast-peers` | Belt-and-braces redundant with the above. coturn evaluates them first. |
| `use-auth-secret` + `static-auth-secret` (>= 32 chars) | Brute-force resistance. HMAC-SHA1 over a 32+ char secret is computationally infeasible. |
| `user-quota=12` / `total-quota=1200` | Caps the relay allocations a single (leaked) credential or the whole server can hold. |
| `max-bps=3000000` | Caps bandwidth per session at ~24 Mbps. Bounds bandwidth exfil via a leaked credential. |
| `stale-nonce=600` | 10-minute replay window on captured credentials. |
| `cipher-list=` AEAD GCM only | Refuses CBC ciphers on TURN/TLS; closes BEAST / Lucky13 / padding-oracle class without disabling TLS 1.2. TLS 1.3 negotiates first when both peers support it. |
| `no-cli` | Removes the telnet management interface attack surface. |
| `simple-log` | ANSI-free log stream; clean parsing in journald / operator tools. |

**Deliberate omissions:**

- **No `fail2ban` jail.** coturn does not emit a single log line that
  contains both the source IP and the auth-failure status; the IP is
  on the `New UDP endpoint` line and the failure on a separate
  `session ... incoming packet ... error 401: Unauthorized` line.
  Multiline fail2ban correlation is fragile across coturn versions
  (see fail2ban/fail2ban#2802) and the marginal value over the
  quota + secret-strength + stale-nonce controls is low: HMAC-SHA1
  brute-force is computationally infeasible and credential-leak
  attacks produce legitimate-looking source IPs. See the
  `Explicit non-features` block in
  [internal_docs/operator/data-security-overview.md](../../../../internal_docs/operator/data-security-overview.md).
- **No allow-list (`allowed-peer-ip`) mode.** Catena's TURN serves
  general browser-to-browser calls; allow-list would break the use
  case. Deny-list of every special-purpose IANA range is the correct
  posture.

**Image-update SLA:** the `coturn_image` pin in
[defaults/main.yml](defaults/main.yml) is bumped within 7 days of
upstream release, sooner on a CVE. CVE feed:
[opencve.io/cve/?vendor=coturn_project](https://app.opencve.io/cve/?vendor=coturn_project)
and the upstream GitHub Security Advisories for
[coturn/coturn](https://github.com/coturn/coturn/security/advisories).
Every bump reruns the bench scenarios that exercise the chat-video
path through coturn before the image pin lands on `main`.

## Adding a third chat-video stack (Matrix / Synapse / Element)

The shared-secret auth model (`use-auth-secret` +
`static-auth-secret`) is the same RFC 7635 HMAC-SHA1 REST credential
scheme used by Synapse, the server behind Element. **No coturn role
changes are required** to add a third chat-video stack -- only a
Synapse compose entry (in `catenahq/dokploy-templates`) that wires
the existing `vault_turn_static_auth_secret` through to
`homeserver.yaml`:

```yaml
turn_uris:
  - "turn:turn.<base>:3478?transport=udp"
  - "turn:turn.<base>:3478?transport=tcp"
  - "turns:turn.<base>:5349?transport=tcp"
turn_shared_secret: "<vault_turn_static_auth_secret>"
turn_user_lifetime: 86400000
turn_allow_guests: true
```

Synapse mints per-call usernames as `<unix_ts>:<matrix_user_id>` and
passwords as `base64(HMAC-SHA1(static_auth_secret, username))`, which
is the same derivation NC Talk and Jitsi/JVB use. The single
`static-auth-secret` in coturn validates all three.

**Caveat:** Jitsi/JVB only supports the `auth-secret` mechanism (not
`lt-cred-mech`). If a future change adds long-term-credential users to
coturn, do not switch the daemon away from `use-auth-secret` -- run
both mechanisms or keep `use-auth-secret` exclusively.

The Synapse template work itself is tracked in
[BACKLOG_TECHNICAL.md](../../../../../BACKLOG_TECHNICAL.md).

## Runbook -- diagnosing a failed Talk / Jitsi call

1. `docker service ls --filter name=coturn` -- replicas 1/1?
2. `ss -uln | grep -E ':(3478|5349)\b'` -- listening?
3. `ufw status verbose | grep -E '(3478|5349|50000)'` -- ufw permitting?
4. `dig +short turn.<base>` -- A record returning the VPS public IP?
   (must NOT be proxied through Cloudflare -- gray-cloud only)
5. `openssl s_client -connect turn.<base>:5349` -- TLS handshake completing?
6. From a client behind a restrictive firewall, capture
   chrome://webrtc-internals during a call: ICE candidates of type
   "relay" must be present.
