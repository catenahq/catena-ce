# The graphical installer

`uv run catena-gui` opens a browser on loopback and walks a client from a bare
server to a working install.

```sh
cd gui
uv run catena-gui --inventory clientco
```

## Where it runs, and what that decides

On the **client's own machine**, as a console process serving a browser UI on
loopback. Whoever runs Ansible holds the SSH private key and every vendor
credential, so a hosted installer would make the operator custodian of every
client's cloud accounts.

The **console owns the job; the browser is a view**. Closing the browser changes
nothing. Closing the console abandons the run, and the run directory says how to
resume. That is structural rather than cosmetic: an install contains two waits
nobody can time -- a server being delivered, and a domain being activated by its
registrar -- and neither fits inside a page load.

## What it is not

Not a second way to install. `install.yaml` plus
`catena install -i ... --no-confirm` is already the declarative, non-interactive
contract, and `seed.py` already live-probes credentials. This is a **third
producer** of that file, beside a person writing it and the test bench rendering
it -- which is what lets a host it built be indistinguishable from one the CLI
built, because it is one.

Not a replacement for the CLI either. `converge`, `recover`, `rollback` and
`show-keyset` are operator verbs that should not need a browser. Recover and
roll back appear here as disabled tabs naming the command to use instead:
`catena recover` re-prompts the whole recovery keyset, because nothing off the
server holds a copy, so it is a different wizard rather than a variant of this
one.

## It holds no knob knowledge

Which questions each page asks comes from
[`../ansible/helpers/knobs.json`](../ansible/helpers/knobs.json), the registry
the on-box store, the installer and the settings page all read. A knob that
declares a `step` appears on that page; one that does not is never asked. There
is no list of fields in this package, and a test refuses one.

Adding a question is therefore a registry edit. Adding a *proof* is a function
in `catena_gui/steps.py`, because every step validates before it advances --
and an installer that collects six pages and discovers on the last one that the
first credential was wrong has spent a client's whole sitting to say something
it knew at the start.

## The run directory

```
~/.catena/gui/
  answers.json     what was answered, 0600. NOT the credentials.
  install.log      what `catena install` printed
```

Credentials are held in memory for the life of the process and written only to
the transient adopt file the CLI already deletes. A resumed run asks for them
again. That is the honest cost of not writing a client's cloud credentials to
their disk: the alternative is a file that outlives the install and that nothing
ever comes back to remove.

## Running it without a UI

The path the test bench drives. It walks the same steps and runs the same
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
