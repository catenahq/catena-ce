# reconcile/roles/swarm

Configures the single-node swarm `bootstrap/roles/docker` creates, on every
converge, from either path: the task-history bound, the `catena.role` label
stateful services constrain to, and the `catena-network-nudge` self-heal.

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
- Installs `/usr/local/bin/catena-network-nudge`, its unit, and the
  `docker.service` drop-in that fires it on every daemon start, so a container
  stranded by the overlay race is started once `catena-network` is back.

## Related

- Upstream: `bootstrap/roles/docker` (the swarm itself).
- Downstream: `traefik`, `postgres`, `portainer` (constrained to the label).
