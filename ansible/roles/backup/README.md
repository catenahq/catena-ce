# backup

Dispatcher for the host's backup pipeline. Wires up restic against
the operator's S3 backup repo, schedules the snapshot timer, and
provides one-shot tasks for verification, restore, and reconciliation.

## Modes (tasks_from)

- `main.yml` (default) -- install restic + the single weekly-backup
  systemd timer (`catena-backup.timer`, Community's only timer, capped
  at weekly cadence) + backup wrapper script, register the Healthchecks
  ping, ensure restic repo is initialized. On a Business host the EE
  catena-daily engine masks this timer and schedules daily/sub-daily
  itself.
- `verify.yml` -- run a dry-restore against the latest snapshot
  into a scratch dir; verify file count and size; emit alert on
  drift.
- `restore.yml` -- full filesystem restore from a chosen snapshot.
  catena-postgres is restored raw (its vault-derived password makes a
  byte-for-byte restore correct); per-app DBs are restored raw and then
  reconciled by a fresh `pg_dumpall` replay (scope=clients), which is
  the `catena-recovery` host binary, not a mode of this role.
- `ensure_restic.yml` -- apt-install + binary version pin only.

## Inputs

- `vault_restic_password` -- restic repository password.
- `vault_aws_access_key_id` / `vault_aws_secret_access_key` -- S3
  credentials.
- `backup_restic_repo` -- S3 URL (e.g. `s3:s3.example.com/bucket`).
- `backup_weekly_timer_oncalendar` -- systemd `OnCalendar` for the CE
  weekly-backup timer (default `weekly`; the `backup_weekly_cap` filter
  fails the converge on anything tighter than weekly -- daily and
  sub-daily cadence is the Business lane).
- `backup_schedule` -- Business daily/sub-daily cadence derived from
  `backup_tier`; consumed by the EE catena-daily chain, not the CE timer.

## Idempotency

- All file/systemd resources converge.
- restic init is a no-op against an existing repo.
- The post-restore reconciliation only fires on the marker file
  `restore.yml` drops; `catena-recovery post-restore` clears it after a
  successful pass, so an interrupted recovery is retried.

## Related

- Operator-facing: `runbooks/restore-to-new-vps.md`.
