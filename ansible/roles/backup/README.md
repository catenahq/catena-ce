# backup

Dispatcher for the host's backup pipeline. Wires up restic against
the operator's S3 backup repo, schedules the snapshot timer, and
provides one-shot tasks for verification, restore, and reconciliation.

## Modes (tasks_from)

- `main.yml` (default) -- install restic + systemd timer + backup
  wrapper script, register the Healthchecks ping, ensure restic
  repo is initialized.
- `verify.yml` -- run a dry-restore against the latest snapshot
  into a scratch dir; verify file count and size; emit alert on
  drift.
- `restore.yml` -- full filesystem restore from a chosen snapshot.
  Replays a fresh `pg_dumpall` afterward (raw-volume restore is
  the authoritative path; pg replay reconciles the dokploy-postgres
  vault password).
- `s3_reconcile.yml` -- list the bucket via the S3 API and prune
  snapshots not in the restic index (orphan cleanup).
- `ensure_restic.yml` -- apt-install + binary version pin only.

## Inputs

- `vault_restic_password` -- restic repository password.
- `vault_aws_access_key_id` / `vault_aws_secret_access_key` -- S3
  credentials.
- `backup_restic_repo` -- S3 URL (e.g. `s3:s3.example.com/bucket`).
- `backup_schedule` -- systemd `OnCalendar` for the timer.

## Idempotency

- All file/systemd resources converge.
- restic init is a no-op against an existing repo.
- pg_replay only fires on the post-restore marker file; cleared
  after a successful pass.

## Related

- Operator-facing: `runbooks/postgres-password-reconciler.md`,
  `runbooks/restore-to-new-vps.md`.
