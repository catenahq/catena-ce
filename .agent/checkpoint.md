# Checkpoint: ops issue 449

## Done

- Cause proven from the code (plan.md, "Cause").
- Fix committed: a3007ccd (filter `ansible/playbooks/filter_plugins/swarm_deploy_retry.py`, the attempt file, the
  exhausted-attempts message, tests). Reviewed by fleet:code-reviewer; its three comment findings applied.
- Gates passed: touched unit tests and tree scanners, `ansible-playbook --syntax-check playbooks/reconcile.yml`,
  prose, unicode, core app names, duplicate sentences.
- catena-admin and ops cloned shallow, read-only, for the search; no change needed in either.

## In flight

- Nothing.

## Next

- Coordinator: push, pull request, regenerate the ops audit docs (the new filter file joins the feature grid).

## Hypotheses

1. The retry and the timeout live in catena-admin's engines: ruled out. The quoted line "attempt 4 failed on a
   transient; retrying" is emitted only by catena-ce
   `ansible/reconcile/roles/infrastructure/tasks/_swarm_stack_deploy_attempt.yml` ("report transient, will retry");
   catena-admin has no `infrastructure_deploy_max_attempts` or `swarm_stack_deploy_timeout`.
2. rc 124 is classified transient unconditionally: confirmed. The old expression rendered through ansible-core's
   Templar returns True for rc 124 with a `task: non-zero exit (1)` task state; the new one returns False, and True
   for a task `Preparing`.

## Facts

- Docker engine pin 29.9.2 (`ansible/bootstrap/roles/docker/defaults/main.yml:59`).
- moby docker-v29.9.2 `daemon/cluster/executor/container/controller.go:143-189`: the pull runs inside Prepare
  (task state PREPARING); a failed pull with no local image returns the pull error as the task error.
- moby docker-v29.9.2 `daemon/containerd/image_pull.go`: "pull access denied for ...", "error pulling ...", "no
  matching manifest for ..."; vendored containerd `core/remotes/docker/resolver.go:696,719`: "failed to authorize",
  "failed to do request".
- ops `automation/audit/orphans.yml:400` covers `catena-ce/ansible/playbooks/filter_plugins/*`.
- `ansible/tests/unit/test_infra_data_copy.py:96` reads the task name "deploy with retry on registry transient".

## Commands

- Unit tests: `flock /tmp/catena-gates.lock uv run --directory /var/tmp/issues/449/catena-ce/ansible pytest -q -p no:cacheprovider tests/unit/<file>.py`
- Gates: `flock /tmp/catena-gates.lock env -C /var/tmp/issues/449/catena-ce node ../contracts/scripts/check-prose.mjs`
  (same for check-unicode.mjs, `check-core-app-names.mjs .`).
