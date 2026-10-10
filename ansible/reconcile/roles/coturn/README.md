# reconcile/roles/coturn

Shared TURN/STUN server for the chat-video apps deployed from the catalog:

- **Nextcloud Talk + HPB** -- Talk reaches `turn.<zone>:5349` for
  restrictive-network media relay. The panel's "Wire Nextcloud Talk + HPB"
  action configures it with `occ talk:turn:add`
  (`scripts/nextcloud-talk-hpb-wire.sh`, installed as
  `/usr/local/bin/catena-wire-nextcloud-talk-hpb`).
- **The Jitsi bridges of Rocket.Chat and Element** -- each template
  configures jitsi-videobridge with `JVB_TURN_HOST` from `TURN_HOSTNAME`
  (`coturn_hostname`, default `turn.<zone>`), `JVB_TURN_PORT=5349` and the
  shared `turn_static_auth_secret`.
- **Element's Synapse** -- hands its clients the same relay, from
  `TURN_HOSTNAME` and `TURN_STATIC_AUTH_SECRET` in its environment.

One coturn deployment serves them all. Auth is `static-auth-secret`
based: each app mints its own ephemeral HMAC-SHA1 credentials per call,
and coturn keeps no per-user database.

## What this role does

1. Runs when a consumer (a running Talk HPB or Jitsi bridge container) is
   up or coturn is already deployed, on a host that has a domain. A
   consumer that goes away leaves coturn running and maintained.
2. Declares its ports to the public-port registry (table below).
3. Points the gray-cloud `turn.<zone>` A record at the host's public IP
   through the Cloudflare API.
4. Issues and renews a TLS cert for `turn.<zone>` via Let's Encrypt
   DNS-01 (Cloudflare API). HTTP-01 is unavailable because no web port is
   open on the host: web traffic arrives through the Cloudflare tunnel.
   certbot's stock systemd timer renews it, and a deploy hook SIGHUPs the
   running coturn container so a renewed cert loads without dropping calls.
   With no Cloudflare token in the store, the A record and the cert wait
   and coturn serves STUN only.
5. Renders `turnserver.conf` from the role's Jinja template,
   parameterized per host (public IP, static-auth secret, hostname,
   relay port range).
6. Creates coturn as a global swarm service on the host network, so the
   container sees the public IP directly (required for ICE-candidate
   advertisement). The tier-1 host engine carries the service spec
   (catena-admin `payload/engines/tier1/catalog.go`, read with
   `catena-tier1 spec coturn`) and reconciles it.

## Why a direct public path

The Cloudflare tunnel carries TCP/HTTP only, and the chat-video media
plane is UDP. All web traffic enters through the tunnel; the UDP media
plane is reached directly on the public IP, through coturn at
`turn.<zone>` and the per-app bridge ports below.

## UDP exposure profile

| Port | Proto | Owner | Purpose |
|------|-------|-------|---------|
| 3478 | UDP | coturn | STUN + plain TURN |
| 5349 | TCP+UDP | coturn | TURN/TLS (restrictive-network fallback) |
| 50000-50100 | UDP | coturn | Coturn relay range (ephemeral) |
| 10000 | UDP | Rocket.Chat Jitsi JVB | Jitsi media (when Rocket.Chat is deployed) |
| 10010 | UDP | Element Jitsi JVB | Jitsi media (when Element is deployed) |

coturn's ports are a registry fragment this role writes, which the
public-port reconciler opens in ufw. The last two are swarm `mode: host`
publishes, which dockerd DNATs past ufw's INPUT chain: they are open because
Docker published them and they close when the service stops. Their
`vps.expose.udp` labels declare them to the public-port registry so they
appear in the effective set.

## DR / portability

- Cert: `/etc/letsencrypt/` rides the backup with the rest of `/etc`; a
  host without it issues a new one on its next converge.
- Static auth secret: minted on-box into `/etc/catena/config.json`
  (`turn_static_auth_secret`), same DR path as every other shared
  secret.
- Swarm service: reconciled on every converge from the tier-1 spec.

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
| `denied-peer-ip=::ffff:0.0.0.0-::ffff:255.255.255.255` | CVE-2026-27624 defense in depth -- IPv4-mapped IPv6 bypass of the IPv4 denies. The pinned image carries the upstream fix; the guard protects against a downgrade to one without it. |
| `no-multicast-peers`, and loopback peers refused by default | Belt-and-braces redundant with the above. coturn evaluates them first. |
| `use-auth-secret` + `static-auth-secret` (>= 32 chars) | Brute-force resistance. HMAC-SHA1 over a 32+ char secret is computationally infeasible. |
| `user-quota=12` / `total-quota=1200` | Caps the relay allocations a single (leaked) credential or the whole server can hold. |
| `max-bps=3000000` | Caps bandwidth per session at ~24 Mbps. Bounds bandwidth exfil via a leaked credential. |
| `stale-nonce=600` | 10-minute replay window on captured credentials. |
| `cipher-list=` AEAD GCM only | Refuses CBC ciphers on TURN/TLS; closes BEAST / Lucky13 / padding-oracle class without disabling TLS 1.2. TLS 1.3 negotiates first when both peers support it. |
| `dtls` | Binds the UDP half of the TLS port, which the public-port fragment declares. |
| No `--cli` | The telnet management interface is opt-in and stays off. |
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
  attacks produce legitimate-looking source IPs.
- **No allow-list (`allowed-peer-ip`) mode.** Catena's TURN serves
  general browser-to-browser calls; allow-list would break the use
  case. Deny-list of every special-purpose IANA range is the correct
  posture.

**Image updates:** the coturn image pin lives in the tier-1 host
engine's built-in catalog (catena-admin
`payload/engines/tier1/catalog.go`), where Renovate and Trivy track it
with the other tier-1 images. CVE feed:
[opencve.io/cve/?vendor=coturn_project](https://app.opencve.io/cve/?vendor=coturn_project)
and the upstream GitHub Security Advisories for
[coturn/coturn](https://github.com/coturn/coturn/security/advisories).

## Element (Synapse)

The shared-secret auth model (`use-auth-secret` +
`static-auth-secret`) is the same RFC 7635 HMAC-SHA1 REST credential
scheme Synapse, the server behind Element, uses. The Element template in
`catenahq/catena-templates` wires `turn_static_auth_secret` into
`homeserver.yaml`, and coturn needs nothing per app:

```yaml
turn_uris:
  - "turn:turn.<zone>:3478?transport=udp"
  - "turn:turn.<zone>:3478?transport=tcp"
  - "turns:turn.<zone>:5349?transport=tcp"
turn_shared_secret: "<turn_static_auth_secret>"
turn_user_lifetime: 86400000
turn_allow_guests: true
```

Synapse mints per-call usernames as `<unix_ts>:<matrix_user_id>` and
passwords as `base64(HMAC-SHA1(static_auth_secret, username))`, which
is the same derivation Nextcloud Talk and Jitsi/JVB use. The single
`static-auth-secret` in coturn validates all of them.

**Caveat:** Jitsi/JVB supports the `auth-secret` mechanism only, and
`lt-cred-mech` users would need both mechanisms enabled: coturn keeps
`use-auth-secret` either way.

## Runbook -- diagnosing a failed Talk / Jitsi call

1. `docker service ls --filter name=coturn` -- replicas 1/1?
2. `ss -uln | grep -E ':(3478|5349)\b'` -- listening?
3. `ufw status verbose | grep -E '(3478|5349|50000)'` -- ufw permitting?
4. `dig +short turn.<zone>` -- A record returning the host's public IP?
   (must NOT be proxied through Cloudflare -- gray-cloud only)
5. `openssl s_client -connect turn.<zone>:5349` -- TLS handshake completing?
6. From a client behind a restrictive firewall, capture
   chrome://webrtc-internals during a call: ICE candidates of type
   "relay" must be present.
