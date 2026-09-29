# The graphical installer

`uv run catena-gui` opens a browser on loopback and walks a client from a bare
server to a working install. Run it from the repository root, or from here:

```sh
uv run catena-gui                      # start on the inventory page
uv run catena-gui --inventory clientco # open or create one directly
```

## Where it runs, and what that decides

On the **client's own machine**, as a console process serving a browser UI on
loopback. Whoever runs Ansible holds the SSH private key and every vendor
credential, so a hosted installer would make the operator custodian of every
client's cloud accounts.

The **console owns the job; the browser is a view**. Closing the browser changes
nothing. Closing the console abandons the run, and reopening its inventory
resumes it. That is structural rather than cosmetic: an install contains two waits
nobody can time -- a server being delivered, and a domain being activated by its
registrar -- and neither fits inside a page load.

## What it is not

Not a second way to install. `install.yaml` plus
`catena-cli install -i ... --no-confirm` is already the declarative, non-interactive
contract, and `seed.py` already live-probes credentials. This is a **third
producer** of that file, beside a person writing it and the test bench rendering
it -- which is what lets a host it built be indistinguishable from one the CLI
built, because it is one.

Not a replacement for the CLI either. `converge` and `show-keyset` are
operator verbs that should not need a browser. Recovering a server is this
install followed by a restore from the panel, so it needs nothing else here.

## It holds no knob knowledge

Which questions each section asks comes from
[`../ansible/helpers/knobs.json`](../ansible/helpers/knobs.json), the registry
the on-box store, the installer and the settings page all read. A knob that
declares a `step` appears in that section, with its explanation behind a `(?)`
tooltip; one that does not is never asked. There is no list of fields in this
package, and a test refuses one.

Adding a question is therefore a registry edit. Adding a *proof* is a function
in `catena_gui/steps.py`: every section has a Check whose results show under
its button, and Install runs every section's check before it starts. An
installer that collects every answer and discovers at the end that the first
credential was wrong has spent a client's whole sitting to say something it
knew at the start.

## An inventory is the run

Two pages. The first lists the directories under `../ansible/inventory/` (the
shipped `example` excluded), one per line, and creates new ones. The second
holds every section in one form. Every field starts from the registry's
default, and a knob that declares an `example` shows it as a placeholder, never
as a value. Each Check saves the whole form:

```
ansible/inventory/<name>/
  .env                the answers, written by seed.py's own writer: the file
                      `catena-cli install` reads. NOT the credentials.
  .catena-gui.json    the last section checked, the install's state, the
                      acknowledgement. 0600.
```

Credentials are held in memory for the life of the process and reach
`catena-cli install` only through a transient 0600 `install.yaml` outside the
inventory, deleted when the install ends. A reopened inventory asks for them
again. That is the honest cost of not writing a client's cloud credentials to
their disk: the alternative is a file that outlives the install and that nothing
ever comes back to remove.

What the install prints goes to the console and to the page from memory, never
to a file: it ends with the passwords the server shows once.

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
