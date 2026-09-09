# reconcile/roles/payload

Installs the catena host engine payload -- the Go binaries every later role
dispatches -- onto the VPS, by extracting them from the public catena-admin
image. No container is deployed here and Portainer is not involved.

## Why it runs at position 5.5

The payload used to arrive from `reconcile/roles/catena-admin`, role 13 in `site.yml`.
Three roles that run BEFORE it already depend on the engines:

- `reconcile/roles/cloudflare_tunnel` (9) dispatches `catena-cloudflared-sync`,
- `reconcile/roles/keycloak` (10) and `reconcile/roles/oauth2_proxy` (11) need the edge that
  engine brings up -- oauth2-proxy waits on
  `https://auth.<zone>/.well-known/openid-configuration`.

So a first converge on a host that already held a Cloudflare API token
deferred the tunnel ("engine not installed yet"), and oauth2-proxy then failed
against an edge nobody had configured. The tokenless install shape hid it: the
tunnel is deferred there for a legitimate reason, the client enters the token
in catena-admin later, and the panel fires `cloudflared-sync` itself.

Running the payload install straight after `bootstrap/roles/docker` -- the only thing it
needs -- removes the window. Every role from 6 onward can assume
`/usr/local/bin/catena-*` exists.

## What it does

1. Asks swarm which image the `catena-admin` service runs and installs the
   engines from that. Falls back to `catena_payload_image` only when there is
   no service to follow -- a first converge, or an explicit
   `CATENA_PAYLOAD_IMAGE`.
2. Pulls that image.
3. Resolves the image ID and compares it with `/etc/catena/.payload-image`.
   Equal means the installed engines already came from this image and the role
   stops there -- so a re-converge changes nothing.
4. Otherwise: `docker create` a throwaway container, `docker cp` the payload
   tree out of it, remove it, restore the directory's ownership, and run the
   image's own `install-ee-payload.sh`.
5. Writes the image ID into the marker.

Step 1 is the reason the two halves cannot drift apart. The engines and the
shell come out of one image because there is one place the version is decided:
the service spec. `reconcile/roles/catena-admin` reconciles hardening, environment and
secrets, never the image -- so when this role resolved `max(floor, pin)`
independently, raising the shipped floor reinstalled the engines and left the
shell on the image it was created with. Following the spec removes the second
input rather than adding a check against it.

`install-ee-payload.sh` installs binaries, python lib modules, post-restore
hooks and systemd units, and does NOT enable any unit. Enabling is the
`catena-daily` engine's job, which keeps install and activate two separate
observable steps.

## Ownership

The catena-admin container mirrors its embedded payload into
`{{ catena_payload_dir }}` at startup, as its own nonroot uid. `docker cp`
writes as root. The role captures the directory's ownership before the copy
and restores it after, or the container's next startup sync fails silently and
the host drifts onto stale engines.

## Marker semantics

The marker holds an image ID, not a timestamp or a bare "installed" flag:

- a re-converge against the same image reinstalls nothing (idempotency),
- an image upgrade reinstalls every engine (that is the update path),
- an engine deliberately placed on the host out of band survives a converge as
  long as the image has not moved. `activate_ee` ships a build-STAMPED
  `catena-daily` carrying the run's operator public key; an unconditional
  reinstall would replace it with an unstamped build and the scenario would
  then pass or fail for a reason unrelated to what it asserts.

## Bench

The bench builds `local/catena-admin:bench` ON the VPS, after the converge,
and stages the payload out of band from that image. It therefore sets
`CATENA_PAYLOAD_INSTALL=false` (and `CATENA_PAYLOAD_PULL=false`): pulling the
published GHCR tag on a bench VM would exercise the last published image
instead of the working tree, which is the one thing a bench must never do.

## Variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `catena_payload_image` | `{{ catena_admin_image }}` | fallback image, used only when no `catena-admin` service exists to follow |
| `catena_admin_service_name` | `catena-admin` (bootstrap/roles/common) | the service whose image the engines follow |
| `catena_payload_dir` | `/var/lib/catena/ee-payload` | extraction target (shared with the container's sync) |
| `catena_payload_image_path` | `/usr/local/share/catena-ee` | payload tree inside the image |
| `catena_payload_marker` | `/etc/catena/.payload-image` | image ID the installed engines came from |
| `catena_payload_pull` | `CATENA_PAYLOAD_PULL`, `true` | pull before extracting |
| `catena_payload_install` | `CATENA_PAYLOAD_INSTALL`, `true` | run the install at all |
| `catena_payload_image_digest` | `CATENA_PAYLOAD_IMAGE_DIGEST`, else `catena_admin_release.digest` | digest the image must resolve to before anything is extracted |

## Which digest this asserts

`catena_admin_release` is resolved from the registry once per converge by
`playbooks/tasks/load_onbox_config.yml`, version and digest together. It
replaced two literals in `bootstrap/roles/common/defaults` that had to be bumped in
lockstep and were not: a version published as one digest with another digest
recorded beside it fails this gate on a correct host holding a correctly
published image, which is what it did.

The check is therefore a same-converge consistency check -- the image about to
be extracted is the one this converge resolved -- not provenance. It catches a
tag moved mid-converge and a mismatched override; it does not catch a
compromised registry. Provenance is `cosign verify` against the keyless
signature `publish-image.yml` records, which needs cosign on the host and is
not wired up.

An image this converge did not resolve (the bench's own build, a lane pin)
extracts with the digest empty, and the role says out loud that it went
unchecked rather than failing closed on a correct host.
