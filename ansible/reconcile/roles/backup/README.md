# backup

Dispatcher for the host's backup pipeline. Wires up restic against
the operator's S3 backup repo, installs the snapshot timer, and
provides a one-shot verification task. Restores run in the
`catena-recovery` host binary, driven from the panel.

## Modes (tasks_from)

- `main.yml` (default) -- write the backup configuration and the
  `catena-backup.timer` unit (installed, never enabled here), register the
  Healthchecks ping, and purge any apt restic or rclone. Whether the timer
  runs, and when, is `catena-schedule apply` reading
  /etc/catena/config.json, and it enables no lane without an active licence.
- `verify.yml` -- run a dry-restore against the latest snapshot
  into a scratch dir; verify file count and size; emit alert on
  drift.

## Where the scripts come from

This role renders the per-host CONFIGURATION -- `backup.env`,
`offsite.env`, `backup-paths`, the exclude patterns, the coverage
paths and every systemd unit. The code that reads them is not here:
`catena-backup-run`, `catena-backup-coverage`, `catena-restic-env`,
`catena-restic-mount`, `catena-restic-unmount`, `catena-restic-short-id`,
`catena-snapshot-export` and `catena-snapshot-list` are lane scripts in
the catena-admin image payload, installed by `reconcile/roles/payload` right after
`bootstrap/roles/docker`. A fix to any of them reaches a host by bumping the image,
the same way every host engine already does. `restic` and `rclone` themselves
ship in the same payload, pinned by digest in the catena-admin `Dockerfile`, and
`catena-backup-run` creates the repository the first time it finds none.

`catena-disk-preflight` is the exception: this role installs it from
`ansible/scripts/disk-preflight.sh`.

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

- `backup_restic_password` -- restic repository password.
- `backup_s3_access_key` / `backup_s3_secret_key` -- S3 credentials.
- `backup_restic_repo` -- S3 URL (e.g. `s3:s3.example.com/bucket`).

All four come from the on-box store, where catena-admin > Settings > Backup
writes them after the install.
Cadence and retention are NOT inputs to this role. Both are set per host
in the catena-admin panel, stored in `/etc/catena/config.json`, and
applied by `catena-schedule` -- which is the only thing that enables a
timer, writes a timer OnCalendar drop-in, or renders
`/etc/catena/backup-retention.env`. This role installs the units and
enables nothing; the converge asserts the Community cap against the
stored value.

## Idempotency

- All file/systemd resources converge.
- The wrapper initializes a repository only where restic reports none
  (exit 10), and stops on a wrong password (exit 12) rather than
  initializing over an intact repository.
