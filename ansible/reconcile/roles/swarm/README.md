# reconcile/roles/swarm

Configures the single-node swarm `bootstrap/roles/docker` creates, on every
converge, from either path: the task-history bound, the `catena.role` label
stateful services constrain to, the private overlays of Healthchecks,
Beszel and Portainer, and the `catena-network-nudge` self-heal.

## Inputs

- `swarm_task_history_limit` -- exited-task retention per service; 2.
- `swarm_node_role_label` -- the `catena.role` label; `data`.
- `swarm_network_nudge_timeout` -- seconds the nudge waits for the overlay to
  re-materialize after a `docker.service` start.

## Side effects

- `docker swarm update --task-history-limit`, only when the limit in force
  differs.
- `docker node update --label-add catena.role=<label>`, only when the label
  differs. The label lives in the swarm raft, which no snapshot carries.
- `docker network create --driver overlay` for `healthchecks_network`,
  `beszel_network` and `portainer_network`, each when it is absent.
  Healthchecks takes the signed-in user from a header anyone who reaches it
  can set, so it is on its network instead of `catena-network`, with only the
  services that call it: its sign-in gate, the panel, Gatus and the Beszel
  alert shim. Beszel's hub signs in any realm user who reaches it and its
  superuser holds the shared admin password, so the hub and its alert shim are
  on theirs, with the hub's gate, the panel and Gatus. Portainer's login takes
  the shared admin password, so it is on its own, with its gate and the panel,
  whose App Templates catalog it reads. Not attachable, so no standalone
  container joins any of them.
- Installs `/usr/local/bin/catena-network-nudge`, its unit, and the
  `docker.service` drop-in that fires it on every daemon start, so a container
  stranded by the overlay race is started once `catena-network` is back.

## Related

- Upstream: `bootstrap/roles/docker` (the swarm itself).
- Downstream: `traefik`, `postgres`, `portainer` (constrained to the label);
  `portainer`, `oauth2_proxy`, `infrastructure`, `catena-admin` (join the
  private overlays).
