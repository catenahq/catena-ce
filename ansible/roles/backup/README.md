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
  catena-postgres is restored raw (its store-derived password makes a
  byte-for-byte restore correct); per-app DBs are restored raw and then
  reconciled by a fresh `pg_dumpall` replay (scope=clients), which is
  the `catena-recovery` host binary, not a mode of this role. It also
  records `min_dump_time` in the post-restore marker: the time of the
  backup BEFORE the one restored, which is the staleness floor the replay
  uses to refuse an archive an earlier pass left behind. The restored
  snapshot's own time cannot serve -- the archives are written before
  `restic backup` runs, so all of them predate it.
- `ensure_restic.yml` -- apt-install + binary version pin only.

## Where the scripts come from

This role renders the per-host CONFIGURATION -- `backup.env`,
`backup-worm.env`, `backup-paths`, the exclude patterns, the coverage
paths and every systemd unit. It no longer ships the code that reads
them: `catena-backup-run`, `catena-backup-coverage`, `catena-restic-env`,
`catena-restic-mount`, `catena-restic-unmount`, `catena-restic-short-id`,
`catena-snapshot-export` and `catena-snapshot-list` are lane scripts in
the catena-admin image payload, installed by `roles/payload` right after
`roles/docker`. A fix to any of them reaches a host by bumping the image,
the same way every host engine already does.

`catena-disk-preflight` is the exception and is still copied here.
`restore.yml` calls it on a fresh disaster-recovery box that has run
`common`, `storage` and `backup` and has no docker -- so there is no
image to extract a payload from at that point. A script has to live
where its earliest caller can reach it.

`install.yml` fails loudly when the wrapper is absent rather than
installing a timer that points at nothing.

## Logical dumps

`catena-backup-run` writes a logical dump per database engine before the
snapshot, alongside the raw volumes (which are also in the set):

- Postgres -> `backup-staging/pg/<container>-<ts>.sql.gz` (`pg_dumpall`).
  Replayed by `catena-recovery`.
- MySQL / MariaDB -> `backup-staging/mysql/<container>-<ts>.sql.gz`
  (`mariadb-dump`, falling back to `mysqldump`). A SEPARATE directory on
  purpose: the replay feeds every `*.sql.gz` under `pg/` to psql.

A dump failure is fatal for the run. It means the engine did not answer,
and snapshotting its volume in that state while reporting success is how
a broken database becomes a broken backup nobody looked at.

## Coverage

`catena-backup-coverage` runs after the snapshot and exits 2 when a running
container bind-mounts a source outside `backup_paths`. The wrapper fails
the run on that (rc 4, which pings the operator lane), so an application
writing where no snapshot reaches is a page rather than a line in a green
run's journal. It runs AFTER the backup deliberately: the covered data is
already captured, so the finding costs a page and not a snapshot. Any
other non-zero from the checker -- including a timeout -- stays non-fatal,
because an unreadable answer is not evidence of a gap.

## Inputs

- `vault_restic_password` -- restic repository password.
- `aws_access_key_id` / `aws_secret_access_key` -- S3
  credentials.
- `backup_restic_repo` -- S3 URL (e.g. `s3:s3.example.com/bucket`).
- `backup_weekly_timer_oncalendar` -- systemd `OnCalendar` for the CE
  weekly-backup timer (default `weekly`; the `backup_weekly_cap` filter
  fails the converge on anything tighter than weekly -- daily and
  sub-daily cadence is the Business lane).
Cadence and retention are NOT inputs to this role. Both are set per host
in the catena-admin panel, stored in `/etc/catena/config.json`, and
applied by `catena-schedule` -- which is the only thing that enables a
timer, writes a timer OnCalendar drop-in, or renders
`/etc/catena/backup-retention.env`. This role installs the units and
enables nothing; the converge asserts the Community cap against the
stored value.

## Idempotency

- All file/systemd resources converge.
- restic init is a no-op against an existing repo.
- The post-restore reconciliation only fires on the marker file
  `restore.yml` drops; `catena-recovery post-restore` clears it after a
  successful pass, so an interrupted recovery is retried.

## Related

- Operator-facing: `runbooks/restore-to-new-vps.md`.
