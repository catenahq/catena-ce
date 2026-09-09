# storage

Dispatcher for primary host storage. Two mutually-exclusive modes
plus an optional bulk tier.

## Modes (tasks_from)

- `built_in.yml` -- root filesystem subdirectories under `/var/lib`
  + `/mnt/data` symlink to root. For VPS instances with a single
  attached disk and no separate block volume.
- `attached.yml` -- mount a separate block device (default
  `/dev/sdb`) at `/mnt/data`, mkfs ext4 if blank, configure
  /etc/fstab. The Docker daemon's `data-root` is fed by the
  `docker` role using whatever path this role provides.
- `bulk.yml` -- optional second tier (e.g., a Public Cloud
  Block Storage attached for cold/Nextcloud data). Mounted at
  `/mnt/bulk` independently from the primary.

## Dispatch

`main.yml` selects between built_in and attached based on
`storage_mode` (default: detect -- attached if `/dev/sdb` is
present and unformatted; otherwise built_in). Bulk runs whenever
`storage_bulk_enabled=true`.

## Inputs

- `storage_mode` -- `built_in` | `attached` | `auto`.
- `storage_attached_device` -- defaults to `/dev/sdb`.
- `storage_bulk_enabled` / `storage_bulk_device` /
  `storage_bulk_mount`.

## Idempotency

- mkfs only fires on blank devices (blkid check).
- /etc/fstab is patched in place; mount is idempotent.

## Related

- Downstream: `docker` consumes `docker_data_root` derived from
  `/mnt/data`.
