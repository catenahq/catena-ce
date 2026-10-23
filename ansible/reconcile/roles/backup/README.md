# backup

Writes the configuration the payload's backup lane reads and takes
the first snapshot. Verifying a backup and restoring it run in the
`catena-recovery` host binary, driven from the panel. A whole-server
restore's converge skips the first snapshot (tag `first_backup`;
`tasks/install.yml` says why).

## Tasks

- `main.yml` -- write the backup configuration, register the
  Healthchecks ping, and purge any apt restic or rclone. Whether the
  payload's `catena-backup.timer` runs, and when, is `catena-schedule apply`
  reading /etc/catena/config.json, and it enables a lane only on a host whose
  edition covers it: the backup runs at most once a week on every edition, and
  more often on Catena Pro.
- `validate.yml` -- the role's checks, which `playbooks/validate.yml` runs:
  the configuration files, the payload's scripts and units, and the
  repository answering, or, while the backups wait after a restore,
  reports that it does not.

## Where the scripts come from

This role renders the per-host CONFIGURATION -- `backup.env`,
`offsite.env`, `backup-paths`, the exclude patterns and the coverage
paths. The code that reads them is not here: `catena-backup-run`,
`catena-backup-coverage`, `catena-restic-env`,
`catena-restic-mount`, `catena-restic-unmount`,
`catena-snapshot-export` and `catena-snapshot-list` are lane scripts in
the catena-admin image payload, installed by `reconcile/roles/payload` right after
`bootstrap/roles/docker`, together with the static units
`catena-backup.service`/`.timer` and `catena-restic-check-subset.service`/`.timer`.
A fix to any of them reaches a host by bumping the image, the same way every
host engine already does. `restic` and `rclone` themselves ship in the same
payload, pinned by digest in the catena-admin `Dockerfile`.

The repository is created when the destination is saved in catena-admin >
Settings > Backup (`../../../scripts/catena-restic-key.py`, op `init`), never
by a backup: `catena-backup-run` stops when it finds no repository.

`install.yml` fails loudly when a converge that installs the payload
leaves the wrapper absent, because the payload's units would then point at
nothing.

## Logical dumps

`catena-backup-run` writes a logical dump per database engine before the
snapshot, alongside the raw volumes (which are also in the set). The dumps are
plain SQL: the backup repository compresses them, so each snapshot after the
first stores only what changed.

- Postgres -> `backup-staging/pg/<container>-<ts>.sql` (`pg_dumpall`).
  Replayed by `catena-recovery`.
- MySQL / MariaDB -> `backup-staging/mysql/<container>-<ts>.sql`
  (`mariadb-dump`, falling back to `mysqldump`). A SEPARATE directory on
  purpose: the replay feeds every export under `pg/` to psql.

The first backup of each day, and the offsite copy before it copies, removes
the snapshots past the retention numbers and checks the repository.

A dump failure is fatal for the run. It means a running engine did not
answer, and snapshotting its volume in that state while reporting success is
how a broken database becomes a broken backup nobody looked at.

A database that is not running yet is another case: the backup waits up to ten
minutes for it. One still not running after that is left out, the backup of
everything else goes ahead, and the run is reported as failed, naming the
application.

## Excluded volumes

`backup_exclude_patterns` lists the infrastructure's disposable paths. An
application keeps its own regenerable data out of the snapshot by labelling the
top-level volume `vps.backup.exclude: "true"` in its compose file;
`catena-backup-run` asks Docker for the labelled volumes on every run and adds
an exclude for each, whatever the stack is called.

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

That matters most for an application's own object storage, where it keeps
file versions: its own retention setting bounds that live bucket's
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
writes them after the install; the password is generated there. The role
writes the restic configuration only once all four are set.
Cadence and retention are NOT inputs to this role. Both are set per host
in the catena-admin panel, stored in `/etc/catena/config.json`, and
applied by `catena-schedule` -- which is the only thing that enables a
timer, writes a timer OnCalendar drop-in, or renders
`/etc/catena/backup-retention.env`. This role enables nothing.

## Idempotency

- All file/systemd resources converge.
- The wrapper stops where restic reports no repository (exit 10) and on a
  wrong password (exit 12); it never initializes one.
