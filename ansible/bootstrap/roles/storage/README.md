# storage

The product's data prefix, and an optional remote bulk tier.

## What it creates

- `built_in.yml` -- `/srv/catena` on the disk the host already has, with
  `apps`, `backup-staging` and `docker` under it. A real directory, not a
  symlink. No filesystem is made and nothing is mounted.
- `bulk.yml` -- an optional second tier over CIFS or NFS (a Storage Box, a
  client NAS), mounted at `/mnt/bulk`. Runs when `storage_bulk_enabled` is
  true, independently of everything above.

## One primary tier

Catena does not manage block devices. A client who needs more space mounts a
disk themselves, bind-mounts it into the app, and declares the path in
`backup_extra_paths` so the backup set and the nightly coverage lane both know
about it.

That solves coverage, not capacity, and the limits are worth knowing before the
first photo library arrives: Docker named volumes always live under the prefix,
so an app has to use bind mounts to reach a separate disk, and restic-ing a
large library plus its off-site copy is a real monthly bill.

## Inputs

- `storage_mount_point` -- the prefix. `/srv/catena`, from
  `playbooks/group_vars/all/main.yml`.
- `storage_bulk_enabled`, `storage_bulk_mount_point`, `storage_bulk_type`,
  `storage_bulk_remote`, `storage_bulk_mount_options`.
- `storage_bulk_username` / `storage_bulk_password` in the on-box store, for
  CIFS. NFS authenticates by source IP and needs neither.

## Idempotency

Directories are created with the mode their consumer expects, including the
`0710` dockerd forces on its data-root, so neither Ansible nor the daemon
fights the other on a re-run. The bulk mount patches `/etc/fstab` in place.

## Related

- `docker` derives `docker_data_root` from `storage_mount_point`.
- `reconcile/roles/backup` names the same prefix in `backup_paths`.
