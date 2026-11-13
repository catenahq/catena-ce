# Plan: ops issue 449, a swarm deploy that cannot converge fails at its first timeout

## Cause (proven)

- `ansible/reconcile/roles/infrastructure/tasks/_swarm_stack_deploy_attempt.yml:178`: the classifier sets
  `_swarm_deploy_transient` true whenever `rc == 124`, whatever the per-task state says.
- `ansible/reconcile/roles/infrastructure/defaults/main.yml:56` (5 attempts) and `:234` (600 s): a task that never
  converges (crash loop, `no suitable node`) burns 5 x 600 s.
- `ansible/reconcile/roles/infrastructure/tasks/swarm_stack.yml:86-94`: the exhausted-attempts failure prints only
  "failed all N attempts on registry transients" and the reproduce line, no per-task state.
- `_swarm_stack_deploy_attempt.yml:228-235`: the retry line ("attempt N failed on a transient; retrying") is the
  one the issue quotes, and names no cause.
- The Keycloak stack reaches this file through `reconcile/roles/keycloak/tasks/deploy.yml:103`; catena-admin runs
  it through `catena-converge` (reconcile.yml). catena-admin's own stack deploys (`payload/engines/apps/deploy.go`,
  `payload/lib/clients_provisioner.py`) are separate paths with their own pull retry, not this loop.
- Docker 29.9.2 (`bootstrap/roles/docker/defaults/main.yml:59`): a task pulls its image in state PREPARING
  (moby `daemon/cluster/executor/container/controller.go` Prepare); a failed pull with no local image fails the
  task with the pull error itself (same file, lines 164-188), older engines with "No such image".

## Steps

1. [Open] Filter `ansible/playbooks/filter_plugins/swarm_deploy_retry.py`: `swarm_deploy_transient(rc, output,
   tasks, converged=False)`. rc 124 is transient only when the per-task state shows a task Preparing, a pull error
   or a registry transient; other rcs keep the registry/daemon transient patterns over output and task state.
2. [Open] `_swarm_stack_deploy_attempt.yml`: the classifier calls the filter; the retry line and the fail-hard
   message carry the per-task state; the fail-hard message names a timeout with no pull as its cause.
3. [Open] `swarm_stack.yml`: the exhausted-attempts message carries the last attempt's rc, stderr and per-task
   state. Task names unchanged (`test_infra_data_copy.py` reads one).
4. [Open] `defaults/main.yml` comment on `swarm_stack_deploy_timeout`.
5. [Open] Tests: new `ansible/tests/unit/test_swarm_deploy_retry.py` (filter behaviour + wiring + message);
   `test_swarm_deploy_is_bounded.py` and `test_swarm_addr_conflict_reclaim.py` move their pattern checks onto the
   filter.
6. [Open] Gates: the touched unit tests, `test_filter_references_resolve.py`, prose, unicode, core-app-names.
7. [Open] Review (fleet:code-reviewer), commit.

## Bench

No scenario asserts the changed messages (searched ops `automation/test_bench`, catena-templates not needed: the
path deploys catena-declared stacks only). The ops generated audit docs gain the new filter file on the next
regeneration.
