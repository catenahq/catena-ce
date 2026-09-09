# reconcile/roles/portainer

Portainer CE -- the container control plane. Spans Docker + Swarm + (later)
Kubernetes;
provides the compose/stack + env + logs + lifecycle API. Routing stays with
`catena-traefik` (Portainer does no reverse proxy).

This role deploys `catena-portainer` (swarm service on catena-network, docker
socket, `/data` BoltDB volume, admin via `--admin-password-file`, tailnet-only
UI). See `defaults/main.yml` + `tasks/main.yml`.

## Verified Portainer API endpoint map

Target pin: **Portainer CE LTS 2.39.4** (`portainer/portainer-ce`). Verified
against `portainer/portainer-skills` + docs.portainer.io (2026-07), NOT guessed.

**Breaking-change gate:** Portainer **2.27.0 removed** the old
`POST /api/stacks?type=&method=&endpointId=` form. On 2.39.4 use the typed
create paths below. Most blog/gist snippets predate this and are wrong.

### Auth
- Fresh-install admin: `--admin-password-file <file>` at container launch (file
  holds the plaintext password; Portainer hashes it). Set once, on an empty
  BoltDB. Alternative (racy, 5-min window): `POST /api/users/admin/init`
  `{Username, Password}` (>= 12 chars).
- JWT (per-run): `POST /api/auth` `{Username, Password}` -> `jwt` ->
  header `Authorization: Bearer <jwt>`.
- Long-lived API token (services): with a JWT,
  `POST /api/users/{id}/tokens` `{description, password}` -> `rawAPIKey`
  (shown once) -> header **`X-API-Key`**.
- Bootstrap flow for `portainer_api_key`: `/api/auth` (JWT) ->
  `/api/users/{id}/tokens` -> rawAPIKey into `/etc/catena/config.json`.

### Environment (endpoint) + swarm id
- List: `GET /api/endpoints` (local Docker/Swarm is usually id `1`).
- Swarm id (swarm-stacks only): `GET /api/endpoints/{envId}/docker/info`
  -> `.Swarm.Cluster.ID`.

### Stack lifecycle (body fields PascalCase; Env is `[{name,value}]`)
| Verb | 2.39.4 |
| --- | --- |
| Create compose-stack (v1 default) | `POST /api/stacks/create/standalone/string?endpointId={id}` `{Name, StackFileContent, Env}` |
| Create swarm-stack (later) | `POST /api/stacks/create/swarm/string?endpointId={id}` `{Name, StackFileContent, Env, SwarmID}` |
| Create from git (App Templates) | `POST /api/stacks/create/{standalone|swarm}/repository?endpointId={id}` `{Name, RepositoryURL, ComposeFilePathInRepository, Env}` |
| Update + redeploy | `PUT /api/stacks/{id}?endpointId={id}` `{StackFileContent, Env, PullImage:true, Prune:false}` |
| Start / Stop stack | `POST /api/stacks/{id}/{start|stop}?endpointId={id}` |
| List / Get / Remove | `GET /api/stacks` / `GET /api/stacks/{id}` / `DELETE /api/stacks/{id}?endpointId={id}` |
| Container logs | `GET /api/endpoints/{id}/docker/containers/{cid}/logs?stdout=1&stderr=1&tail=N` |
| Container start/stop | `POST /api/endpoints/{id}/docker/containers/{cid}/{start|stop}` |

No `composeStatus` field: derive readiness from `GET /api/stacks/{id}`
(`Status`: 1=active, 2=inactive) + container State via
`GET /api/endpoints/{id}/docker/containers/json`.

Stacks have no project grouping: group by stack Name prefix and enumerate
`GET /api/stacks`. There is no domain API either -- routing is our Traefik
file-provider routes (`app-route.yml.j2` / dashboard-sync).

### App Templates (client self-serve marketplace) -- templates.json v3
Entry for a catalog app (`render.py` re-target target):
```json
{ "type": 3, "title": "...", "description": "...", "categories": ["..."],
  "platform": "linux", "logo": "https://.../logo.png", "note": "<p>ok</p>",
  "repository": { "url": "https://github.com/catenahq/<templates-repo>",
                  "stackfile": "blueprints/<id>/docker-compose.yml" },
  "env": [ { "name": "PORT", "label": "Port", "default": "5006",
             "select": [ { "text": "A", "value": "a", "default": true } ] } ] }
```
- `type 3` = compose-stack, deployed FROM a git repo (not inline); requires
  Compose format **version "2"**; env has **no secret generator** (pre-seed
  generated secrets into `env.default` in `render.py`); App Templates do **not**
  create routes (route on new-stack event).

Sources: docs.portainer.io/api/access, /advanced/app-templates/format,
github.com/portainer/portainer-skills (portainer-api/references/stacks.md),
portainer discussions #12670 (2.27.0 removal), hub.docker.com/r/portainer/portainer-ce.
