# reconcile/roles/host_maintenance

The host's own upkeep, applied on every converge from either path, so a
change here reaches a host that converges itself: apt's settings, the host
resolving its own name, the baseline packages, Debian's unattended-upgrades
and its origins, needrestart's restart policy, the journal's storage, the host
mail agent kept off port 25, the settings the client changes in catena-admin >
Settings, and whether this host needs a reboot. It runs right after `payload`.

## Packages, updates, journal, mail agent

- `playbooks/tasks/apt_settings.yml` (the optional HTTP proxy, the dpkg lock
  timeout) and `playbooks/tasks/host_name_resolution.yml` (the hostname on
  `127.0.1.1`, cloud-init kept off `/etc/hosts`), shared with
  `bootstrap/roles/common`, which applies both before its first apt operation.
- `catena_baseline_packages` (in `playbooks/group_vars/all/main.yml`, shared
  with `bootstrap/roles/common`, which installs them at bootstrap).
- `/etc/apt/apt.conf.d/20auto-upgrades` enables unattended-upgrades; its
  service and `apt-daily-upgrade.timer` are enabled. Nothing else on the host
  applies OS packages.
- `/etc/apt/apt.conf.d/51catena-origins` adds the stable point releases, the
  stable-updates suite and Tailscale's repository to Debian's security suite.
  Tailscale's signing key and source list are fetched again on every converge
  where `bootstrap/roles/tailscale` added them.
- `/etc/needrestart/conf.d/catena.conf` sets needrestart to restart the host
  services left on a replaced library after every apt run, and to leave
  `docker` and `containerd` for a restart of the host: under swarm, restarting
  either restarts every container.
- `/etc/systemd/journald.conf.d/10-catena.conf`: persistent, sealed journal.
  The sealing key is minted once by `bootstrap/roles/common`.
- `exim4` is masked, and stopped if running, so port 25 stays free for the
  mail server.

## Time zone and locale

`COMMON_TIMEZONE` and `COMMON_LOCALE` live in the store; the role applies them
on every converge (`timedatectl set-timezone`, then the locale uncommented in
`/etc/locale.gen` and `locale-gen`). Unset, the host runs on America/Toronto
and generates en_CA.UTF-8. A zone the host does not know is reported and left
unapplied, and a locale `/etc/locale.gen` does not list changes nothing, so a
value typed in the panel never fails a converge.

## Why the reboot probe exists

Debian's `unattended-upgrades` applies the OS updates on a Catena host (this
role configures it), and it does not reboot:
`Unattended-Upgrade::Automatic-Reboot` is off, because a reboot is downtime for
every application on the box and choosing when belongs to whoever answers for
that.

That leaves a gap this role fills. A kernel, libc or systemd update installs,
the host reports itself patched, and the running code is still the old code
until somebody boots it. A patch nobody has rebooted into is the same shape of
problem as a backup nobody has verified.

## What it does

`catena-reboot-check.timer` runs `catena-reboot-check` hourly. The script and
both units ship in the catena-admin payload; this role renders
`/etc/catena/reboot-check.env` and enables the timer. The probe reads
`/var/run/reboot-required` and, when `needrestart` is installed, the services
still running on replaced libraries. It writes
`/var/lib/catena/reboot-required.json` and pings the self-hosted Healthchecks
plane: `/fail` while a reboot is pending, success while it is not.

Healthchecks notifies on the transition and escalates through ntfy, so a person
is told once when the host starts needing a reboot and once when it stops. No
second alert path exists to configure, or to forget to configure.

Services on replaced libraries are reported and do not page: needrestart has
already restarted the ones it may, and the ones left (the container engine
among them) wait for the same restart the kernel does.

## What it will never do

Reboot. The probe has no path that boots the host, and adding one would move a
decision about downtime from a person to a timer.
