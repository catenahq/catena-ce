# reconcile/roles/swarm

Configures the single-node swarm `bootstrap/roles/docker` creates, on every
converge, from either path: the task-history bound, the `catena.role` label
stateful services constrain to, the private overlays of Beszel and the alert
intake, and the `catena-network-nudge` self-heal.

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
- `docker network create --driver overlay` for `beszel_network`,
  `beszel_gate_network` and `alert_network`, each when it is absent. The role
  deletes no network, so an overlay a running application names as external
  stays. Anyone on `alert_network` can post to the alert intake, so only the
  intake and the services that post to it are on it, and the panel is not.
  Beszel's hub signs in any realm user who reaches it and its
  superuser holds the shared admin password, so the hub and its alert shim are
  on theirs, with Gatus. The hub takes the signed-in email from its gate's
  header only over `beszel_gate_network`, which only the gate and the hub join;
  Docker picks its subnet.
  Not attachable, so no standalone
  container joins any of them.
- Installs `/usr/local/bin/catena-network-nudge`, its unit, and the
  `docker.service` drop-in that fires it on every daemon start, so a container
  stranded by the overlay race is started once `catena-network` is back.
  Before each daemon start the drop-in also deletes the overlay links
  (`vx-<id>`) a daemon that stopped mid-setup left in the host namespace,
  which would otherwise keep their networks' tasks from starting.

## Related

- Upstream: `bootstrap/roles/docker` (the swarm itself).
- Downstream: `traefik`, `postgres` (constrained to the label);
  `oauth2_proxy`, `infrastructure` (join the private overlays).
