# Checkpoint: ops issue 449

## Done

- Cause proven from the code (plan.md, "Cause").
- catena-admin (`/var/tmp/issues/449/catena-admin`, shallow, default branch) and ops (`/var/tmp/issues/449/ops`,
  shallow, default branch) cloned read-only for the search; no change needed in either.

## In flight

- Plan steps 1-5 (build, in catena-ce).

## Next

- Gates, review, commit.

## Hypotheses

1. The retry and the timeout live in catena-admin's engines: ruled out. The quoted line "attempt 4 failed on a
   transient; retrying" is emitted only by catena-ce
   `ansible/reconcile/roles/infrastructure/tasks/_swarm_stack_deploy_attempt.yml:228-235`; catena-admin has no
   `infrastructure_deploy_max_attempts` or `swarm_stack_deploy_timeout` (token-savior search over the clone).
2. rc 124 is classified transient unconditionally: confirmed, `_swarm_stack_deploy_attempt.yml:178`.

## Facts

- Docker engine pin 29.9.2 (`ansible/bootstrap/roles/docker/defaults/main.yml:59`).
- moby docker-v29.9.2 `daemon/cluster/executor/container/controller.go:143-189`: the pull runs inside Prepare
  (task state PREPARING); a failed pull with no local image returns the pull error as the task error.
- moby docker-v29.9.2 `daemon/containerd/image_pull.go`: pull errors read "pull access denied for ...", "error
  pulling ...", "no matching manifest for ..."; vendored containerd `core/remotes/docker/resolver.go:696,719`:
  "failed to authorize", "failed to do request".
- ops `automation/audit/orphans.yml:400` covers `catena-ce/ansible/playbooks/filter_plugins/*`.
- `ansible/tests/unit/test_infra_data_copy.py:96` reads the task name "deploy with retry on registry transient".

## Commands

- Unit tests (cwd `/var/tmp/issues/449/catena-ce/ansible`, after `uv sync`):
  `flock /tmp/catena-gates.lock uv run pytest -q -p no:cacheprovider tests/unit/<file>.py`
