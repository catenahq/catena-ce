# reconcile/roles/payload

Installs the catena host engine payload -- the Go binaries every later role
dispatches -- onto the VPS, by extracting them from the public catena-admin
image. No container is deployed here and Portainer is not involved.

## Why it runs right after docker

Roles later in `converge.yml` depend on the engines:

- `reconcile/roles/cloudflare_tunnel` dispatches `catena-cloudflared-sync`;
- `reconcile/roles/keycloak` and `reconcile/roles/oauth2_proxy` validate
  against the edge that engine brings up, probing `https://auth.<zone>/`
  through it.

Without the engine, a first converge on a host that already holds a
Cloudflare API token would defer the tunnel and then fail those probes
against an edge nobody configured. A tokenless install defers the tunnel for
a legitimate reason instead: the client enters the token in catena-admin
later, and the panel fires `cloudflared-sync` itself.

So the payload install runs straight after `bootstrap/roles/docker`, the only
thing it needs, and every role after it can assume `/usr/local/bin/catena-*`
exists.

## What it does

1. Asks swarm which image the `catena-admin` service runs and installs the
   engines from that. Falls back to `catena_payload_image` only when there is
   no service to follow -- a first converge, or an explicit
   `CATENA_PAYLOAD_IMAGE`.
2. Refuses the converge, before anything is pulled, copied out or run, when
   `catena_payload_image_digest` is empty: the converge named the image
   without a digest (see below).
3. Pulls that image, unless it is addressed by digest and already on the host:
   the image the panel runs always is, so the host's own converge does not
   depend on the registry answering.
4. Refuses, before anything is copied out or run, an image whose repo digests
   do not include `catena_payload_image_digest`, and says so when they do.
5. Refuses, before anything is copied out or run, an image built from another
   catena-ce tree than the one running this converge: it compares the hash the
   image records in `/usr/local/share/catena-ce/VENDOR.json` with this tree's,
   both named by `helpers/tree_hash.py`. Engines under another version's roles
   leave a file one version hands to the other with no owner.
6. Resolves the image ID and compares it with `/etc/catena/.payload-image`.
   Equal means the installed engines already came from this image and the role
   stops there -- so a re-converge changes nothing.
7. Otherwise: `docker cp` the payload tree out of a throwaway container,
   remove it, restore the directory's ownership, and run the image's own
   `install-ee-payload.sh`.
8. Writes the image ID into the marker.

Step 1 is the reason the two halves cannot drift apart. The engines and the
shell come out of one image because there is one place the version is decided:
the service spec. `reconcile/roles/catena-admin` reconciles the service to
`catena_admin_image`, the image the converge was started with, several roles
after this one; following the spec leaves one input.

`install-ee-payload.sh` installs binaries, python lib modules, dispatch
drop-ins and systemd units, and does NOT enable any unit. Enabling is the
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

## Variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `catena_payload_image` | `{{ catena_admin_image }}` | fallback image, used only when no `catena-admin` service exists to follow |
| `catena_admin_service_name` | `catena-admin` (catena-ce/ansible/playbooks/group_vars/all/main.yml) | the service whose image the engines follow |
| `catena_payload_dir` | `/var/lib/catena/ee-payload` | extraction target (shared with the container's sync) |
| `catena_payload_image_path` | `/usr/local/share/catena-ee` | payload tree inside the image |
| `catena_payload_marker` | `/etc/catena/.payload-image` | image ID the installed engines came from |
| `catena_payload_tree_root` | `{{ playbook_dir }}/../..` | the catena-ce tree this converge runs |
| `catena_payload_tree_record` | `/usr/local/share/catena-ce/VENDOR.json` | where the image records the tree it was built with |
| `catena_payload_pull` | `CATENA_PAYLOAD_PULL`, `true` | pull before extracting |
| `catena_payload_install` | `CATENA_PAYLOAD_INSTALL`, `true` | run the install at all |
| `catena_payload_image_digest` | `CATENA_PAYLOAD_IMAGE_DIGEST`, else the digest `catena_admin_image` carries | digest the image must resolve to before anything is extracted; empty refuses the converge |

## Which digest this asserts

The digest of the release that delivered the tree running this converge, read
off `catena_admin_image` (`CATENA_ADMIN_IMAGE`). An install names its release
as `repo:tag@sha256:...`, the reference
`catena-ce/ansible/helpers/fetch_release.py` resolved
and verified every byte of; a host's own converge (catena-admin
`catena-converge`) names the image the panel service runs by digest: the one
the service spec records, else the one that image carries on the host for its
repository, and refuses an image that carries none. The panel's update rolls
the service to the digest its pull resolved, and refuses a pull that resolves
none.

The check proves that the engines come from the very bytes the running tree
came from. It does not prove provenance: whoever named the digest is trusted,
and when an install is given only a tag that is the registry, so a compromised
registry passes it. Provenance is `cosign verify` against the keyless
signature catena-admin's publish workflow records, which needs cosign on the
host.

An image named by tag alone carries no digest, and the role refuses the
converge. An image built or loaded by hand on the server has none until it is
pushed to a registry and pulled from there.
