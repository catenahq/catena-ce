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
  records `dump_archives` in the post-restore marker: the archives the
  restored snapshot CARRIES, which is what the replay uses to refuse one an
  earlier pass left behind. Timestamps cannot answer that -- an archive is
  written before the `restic backup` that captures it, a host snapshots many
  times between two dumps, and a migrated host's newest archive was written
  on the machine its data came from. An absent line means the listing could
  not be read and leaves the replay unguarded; a present but empty one means
  the snapshot carried nothing and admits nothing.
- `ensure_restic.yml` -- apt-install + binary version pin only.

## Where the scripts come from

This role renders the per-host CONFIGURATION -- `backup.env`,
`offsite.env`, `backup-paths`, the exclude patterns, the coverage
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

## Offsite copy sizing

The offsite-copy lane runs `rclone copy` incrementally -- only new or
changed objects transfer each run, not a full re-push -- on a daily
cadence by default. WHICH buckets it copies is not configured in this
role: the list lives in `/etc/catena/config.json`, written by the panel
and read straight off disk by the lane.

The destination is additive-only by design (ransomware immutability) and
nothing in Catena's tooling ever deletes from it -- Object Lock retention
is a property of the bucket itself, set outside Catena. So every object a
source bucket has ever held since the copy was declared accumulates there
permanently, even after the source's own retention has pruned it.

That matters most for an application's own object storage, where the
application keeps file versions. Nextcloud is the worked example: it
writes directly to S3 as its primary object storage, and
`NEXTCLOUD_VERSIONS_RETENTION` (set in the app template's own Environment
tab in Portainer, not in this installer) bounds that live bucket's
steady-state size. Raising it mainly grows the LIVE bucket, not the
accumulation rate offsite -- a daily copy catches new versions well before
a 7-day or 30-day live-side cap prunes them. The real cost driver over a
deployment's lifetime is cumulative file and version churn, unbounded by
design, with pruning intentionally left to a separate delete-capable
operator tool, never the VPS itself.

A copy of an application's file bucket is only restorable together with a
same-moment copy of the database that indexes those files. That database
is inside the restic snapshots, so a file bucket copied on its own is not
a backup by itself.
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
