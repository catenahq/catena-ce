# Tests

Two kinds, split by what they need to run.

| Directory | Needs | Run by |
| --- | --- | --- |
| `unit/` | Nothing but Python | `uv run pytest` from `../` (pytest's `testpaths`), and the repo's CI on every change |
| `external/` | A live, converged host | `../playbooks/validate.yml`, as its third vantage point |

## unit/

Plain pytest over the installer, the helpers, the filter plugins and the
shipped scripts. No network, no VM, no Ansible run: where a test needs a
converged host it belongs in `external/` or in the maintainers' rehearsal
suite instead.

Several of these read the Ansible tree as data rather than executing it
(asserting that a role's tasks carry `no_log`, that a timer's cadence
stays within the weekly cap, that a template renders a specific field).
That is deliberate. It is how a structural invariant gets a test at all,
given the alternative is a full converge.

## external/

Probes that only mean something from outside the host, run by
`validate.yml` after the on-host and tailnet passes. `public-ports.yml`
scans the server's public IP and fails on any listening port that was not
declared -- the positive assertion that the no-inbound-ports design holds
in reality, not just in the firewall config. `scan_ports.py` is the
scanner it uses, pure Python so no system `nmap` is required.

## What is not here

End-to-end coverage -- install, break, restore, upgrade on disposable
VMs -- runs in the maintainers' rehearsal suite, which is not part of
this repository. What it covers, per feature, is published in
[../../VALIDATION.md](../../VALIDATION.md).
