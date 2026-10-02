# The graphical installer

`uv run catena-gui` opens a browser on loopback and walks a client from a bare
server to a working install. Run it from the repository root, or from here:

```sh
uv run catena-gui                      # start on the inventory page
uv run catena-gui --inventory clientco # open or create one directly
```

## Where it runs, and what that decides

On the **client's own machine**, as a console process serving a browser UI on
loopback. Whoever runs Ansible holds the SSH private key, so a hosted installer
would make the operator custodian of every client's server.

The **console owns the job; the browser is a view**. Closing the browser changes
nothing. Closing the console stops the install with it; reopening its inventory
keeps every answer and offers Install again, as after a failed install. That is structural rather than cosmetic: an install can start with
a wait nobody can time -- a server being delivered -- and it does not fit inside
a page load.

## What it is not

Not a second way to install. `install.yaml` plus
`catena-cli install -i ... --no-confirm` is already the declarative, non-interactive
contract. This is a **third producer** of that file, beside a person writing it
and the test bench rendering it -- which is what lets a host it built be
indistinguishable from one the CLI built, because it is one.

Not where a server is configured. It asks for what reaches and installs the
server: its address, the key that opens it, and the administrator's email. The
domain, the private network and the backups are entered in the panel's Settings
once the server runs, and the installer has no field for any of them.

Not a replacement for the CLI either. `converge` is an operator verb that
should not need a browser. Recovering a server is this
install followed by a restore from the panel, so it needs nothing else here.

## It holds no knob knowledge

Which questions each section asks comes from
[`../ansible/helpers/knobs.json`](../ansible/helpers/knobs.json), the registry
the on-box store, the installer and the settings page all read. A knob that
declares a `step` appears in that section, with its explanation behind a `(?)`
tooltip; one that does not is never asked. There is no list of fields in this
package, and a test refuses one.

Adding a question is therefore a registry edit. Adding a *proof* is a function
in `catena_gui/steps.py`: a section that says what it `validates` has a Check
that observes it, with its results under its button. Install runs every check
before it starts.

The server's check is the one that matters: an SSH server has to answer, and
the install has to be able to log in. Either the key already opens the initial
login -- most providers install it when the server is ordered, and the key
field suggests the pairs already in `~/.ssh` -- or the provider's password
does, and the install uses it once to add the key. The password is the one
field the section has beside the registry's: the install contract's
`host_initial_password`, held in memory only.

Fields the registry marks `required` block until filled; everything else is
labelled optional.

## An inventory is the run

Three pages. The first lists the directories under `../ansible/inventory/` (the
shipped `example` excluded), one per line, and creates new ones. The second
holds every section in one form. Every field starts from the registry's
default, and a knob that declares an `example` shows it as a placeholder, never
as a value. The third, "Reaching the panel", gives the SSH forward as the
`panel` account that opens the panel once the install ends. Each Check saves
the whole form:

```
ansible/inventory/<name>/
  .env                the answers, written by seed.py's own writer: the file
                      `catena-cli install` reads. NOT the credentials.
  .catena-gui.json    the last section checked, the install's state, the
                      acknowledgement. 0600.
```

The provider's password is held in memory for the life of the process and
reaches `catena-cli install` only through a transient 0600 `install.yaml`
outside the inventory, deleted when the install ends. A reopened inventory asks
for it again. That is the honest cost of not writing it to a client's disk: the
alternative is a file that outlives the install and that nothing ever comes
back to remove.

What the install prints goes to the console and to the page from memory, never
to a file: it carries the passwords the server shows once. The page keeps the
block the CLI frames them in, printed right after bootstrap, in its own box
above the output, which scrolls and follows new lines. The install runs with
no stdin: nothing on the page can answer a question, so none is waited on.

## Running it without a UI

The path the test bench drives. It checks the same sections with the same
probes, so a bench green is a shape a client can reach:

```sh
uv run catena-gui --inventory clientco --answers answers.yaml --no-browser
uv run catena-gui --inventory clientco --answers answers.yaml --no-browser --validate-only
```

## Why it lives at the repo root

`scripts/vendor-catena-ce.sh` in catena-admin copies `git ls-files -- ansible`
into the image the panel ships as. Anything under `ansible/` travels into a
public image; this does not belong there, so it sits beside it with its own
`pyproject.toml`.

The root `pyproject.toml` depends on both projects, which is what runs
`catena-gui` and `catena-cli` from the root.
[tests/test_the_entry_points_agree.py](tests/test_the_entry_points_agree.py)
holds the two declarations, the code they name, the launcher's call into the
CLI and every documented `uv run` command to one another.
