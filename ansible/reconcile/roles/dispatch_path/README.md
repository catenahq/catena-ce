# reconcile/roles/dispatch_path

The path a panel request crosses to become a root action on the host, kept on
the generation this tree ships:

- sshd's `AcceptEnv` for the runner account
  (`/etc/ssh/sshd_config.d/30-catena-admin-runner-acceptenv.conf`)
- the runner's sudo rule and its `env_keep` (`/etc/sudoers.d/catena-admin-runner`)
- the forced command on the panel key's line in the runner's `authorized_keys`:
  `../../../scripts/catena-dispatch-entry.sh`, which refuses a request carrying
  more than one action name before sudo runs, then hands the name to
- the dispatcher, `../../../scripts/catena-admin-runner.sh`

A generation is one rendering of all four, named by their hash, its two
executables in `/usr/local/lib/catena-dispatch/<generation>/`.
`/usr/local/sbin/catena-admin-runner` links to the running one's dispatcher,
for root callers that run it directly.

## How a generation changes

`../../../helpers/dispatch_path.py` runs on the host and moves the panel the
way the Lockdown closes public SSH: the new generation is installed beside the
running one (its rules in `-next` files: sudo's `env_keep` and sshd's
`AcceptEnv` add up across files), a key minted for that run and authorized
from loopback for the new entry alone dispatches a proof through it, and only
then does the panel key's line move. The panel's own key proves the result,
and the older generation is removed. A failed proof keeps the running path
and stops the converge.

The proof dispatches `dispatch-path-proof`, which the dispatcher answers
itself: the generation that answered, and which passthrough variables arrived.
A request carrying more than the action name has to be refused by the entry.
The proof key's line carries `expiry-time` ten minutes out, so a run killed
mid-proof leaves no usable key.

A generation already running is trusted only while its directory holds exactly
this tree's two executables (root-owned, mode 0755, no links, nothing else).
Otherwise it is reinstalled and the panel's key proves it before the link moves
or any other generation is removed.

## What it refuses

- A passthrough name that is not `X_FORWARDED_<WORD>` or `CATENA_<WORDS>` in
  capital letters, or that is `CATENA_ADMIN_ACTIONS_FILE`: the names are
  rendered into sudo's `env_keep` and sshd's `AcceptEnv`, which take wildcards,
  and reach a root dispatcher where loader and shell variables change what runs.
- A panel public key that is not exactly one `ssh-ed25519` line.
- A runner `~/.ssh` that is a link or not the runner's, and an `authorized_keys`
  that is not a plain file, has a second name or is owned by neither the runner
  nor root. The runner owns that directory, so root reads and
  writes the file through a descriptor opened without following links, and
  creates its temporary with `O_EXCL`.

The key lines use `restrict` (no forwarding, no pty, no `~/.ssh/rc`) with the
forced command.

## What it never writes

The runner account, the panel's key pair, the host key and the panel's
`known_hosts` are `../../../bootstrap/roles/catena_admin_host`'s, which runs
right before this role in `converge.yml`. The runner's `authorized_keys` holds
the panel's line alone. `../../../boundary.yml` declares this role, under
`proven_switches`, the one reconcile-side writer of the paths above.

## Ordering

First of the reconcile roles in both converge playbooks: a proof that fails
stops the converge before any other role changes the host, and a panel update
gets the path it needs before the rest of its converge runs.

## Variables

`catena_admin_dispatch_env_passthrough` (defaults) is the list of variables
the dispatch may carry. The runner account, its home, the panel's key
directory and the dispatcher's stable path are in
`../../../playbooks/group_vars/all/main.yml`, because the install's half reads
them too.
