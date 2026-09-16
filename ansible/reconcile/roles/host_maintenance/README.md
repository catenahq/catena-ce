# reconcile/roles/host_maintenance

Host-level maintenance reporting. One probe today: whether this host needs a
reboot.

## Why it exists

Debian's `unattended-upgrades` applies the OS security patches on a Catena host
(`bootstrap/roles/common` installs and configures it), and it does not reboot:
`Unattended-Upgrade::Automatic-Reboot` is off, because a reboot is downtime for
every application on the box and choosing when belongs to whoever answers for
that.

That leaves a gap this role fills. A kernel, libc or systemd update installs,
the host reports itself patched, and the running code is still the old code
until somebody boots it. A patch nobody has rebooted into is the same shape of
problem as a backup nobody has verified.

## What it does

`catena-reboot-check.timer` runs `run-reboot-check.sh` hourly. It reads
`/var/run/reboot-required` and, when `needrestart` is installed, the services
still running on replaced libraries. It writes
`/var/lib/catena/reboot-required.json` and pings the self-hosted Healthchecks
plane: `/fail` while a reboot is pending, success while it is not.

Healthchecks notifies on the transition and escalates through ntfy, so a person
is told once when the host starts needing a reboot and once when it stops. No
second alert path exists to configure, or to forget to configure.

Services on replaced libraries are reported and do not page: the next deploy of
that service restarts it, while a kernel is only replaced by a boot.

## What it will never do

Reboot. The probe has no path that boots the host, and adding one would move a
decision about downtime from a person to a timer.
