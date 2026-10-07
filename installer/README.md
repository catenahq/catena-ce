# catena-installer

`catena-installer` installs a Catena server from the client's machine and
opens its panel. With no arguments it serves a browser page on loopback; with
a verb it runs in the console.

```sh
uvx catena-installer                                   # the page
uvx catena-installer install --inventory clientco      # the console
uvx catena-installer install --inventory clientco --release vX.Y.Z
uvx catena-installer connect --inventory clientco      # open the panel here
```

From a checkout of catena-ce, `uv run catena-installer` runs this tree's copy:
the one operators and the test bench run.

## Where it runs, and what that decides

On the **client's own machine**, Windows, macOS or Linux, with
[uv](https://docs.astral.sh/uv/) the only thing to install. Whoever holds the
SSH private key reaches the server, so a hosted installer would make the
operator custodian of every client's server (ADR 0011).

It runs no Ansible itself. It reaches the server over SSH (paramiko), sends a
starter script and the image fetcher through that session, and the server
installs itself with the code of the release it fetched
([../ansible/install-host.sh](../ansible/install-host.sh)): the roles, the
engines and the panel the server ends up with are one version. What the
server prints comes back through the session, to the console or to the page.

The **console owns the job; the browser is a view**. Closing the browser
changes nothing. Closing the console stops the install with it; reopening its
inventory keeps every answer and offers the install again, as after a failed
one. Running the install again on an installed server re-applies the release
it runs, and repairs it.

## The install, step by step

1. Log in: as `ops` with the inventory's key on a server installed before,
   else as the provider's login with the key, else once with the provider's
   password to add the key there. A server presenting another host key than
   the one this machine trusts gets nothing until the client confirms it was
   reinstalled.
2. Leg `accounts`, through that login: the starter installs what the release
   needs to start, fetches the release image by digest and runs its
   install-host.sh, which creates `ops` and the forward-only `panel` account
   with this machine's public key. Nothing is closed yet.
3. Log in again, as `ops` with the key. That login is what the hardening
   waits for: the server checks sshd's record of this very session before its
   first role turns off every other login.
4. Leg `system`: bootstrap, the passwords (printed between two marker lines),
   the converge, the version recorded, the server's own checks.
5. The checks only another machine can make: a scan of every TCP port of the
   public IP against what the server declares, and the containers' network
   out of reach from here.
6. The page opens the `panel` account's forward and links the panel and
   Portainer; `connect` does the same from the console.

`--release vX.Y.Z` moves the server to that release with that release's own
code and records it, the same end state as the panel's update. Without it the
server keeps the release it records, and a new server gets the newest one.

## What the package carries

The installer page and the console are one program (`catena_installer/`). It
carries a few files of the Ansible tree, which a wheel holds under
`catena_installer/tree/` and a checkout reads from `../ansible/`
(`catena_installer/tree.py`): `seed.py` and the knob registry, which write and
read an inventory's `.env`, and the files sent to the server before the
release is there, `helpers/fetch_release.py` and what it imports. The starter
script (`catena_installer/starter.sh`) has nothing version-specific in it, so
the newest installer can install or move a server to any release.

It lives beside `../ansible/` rather than in it because catena-admin's vendor
step copies the tracked `ansible/` tree into the public panel image, and the
installer does not belong there.

## It holds no knob knowledge

The sections, their explanations and the questions each asks come from
[`../ansible/helpers/knobs.yml`](../ansible/helpers/knobs.yml), the registry
the on-box store, the installer and the settings page all read. A knob that
declares a `step` appears in that section under its `label`, with its `help`
behind a `(?)` tooltip; one that does not is never asked. There is no list of
fields in this package, and a test refuses one.

Adding a question is therefore a registry edit. Adding a *proof* is a function
in `catena_installer/steps.py`, registered in its `PROBES` under the section it
checks. The one button verifies every section and starts the install only when
none of them blocks, with what the checks found under it.

The server's check is the one that matters: an SSH server has to answer, with
the host key this machine trusts for it, and the install has to be able to log
in. A server presenting another key gets no login and no password: its check
carries a box to tick when the server was reinstalled. Either the key already
opens the initial login -- most providers install it when the server is
ordered, and the key field suggests the pairs already in `~/.ssh`, or offers to
create the one it names -- or the provider's password does, and the install
uses it once to add the key. The password is the one field the section has
beside the registry's, held in memory only.

Fields the registry marks `required` block until filled; everything else is
labelled optional.

## English and French

Every page is in English or French. The switcher at the end of the tab bar
stores the choice in a cookie; without one, the browser's preferred language
decides, then English. A registry field carries its `label` and `help` in both
languages. The page's own words are in `catena_installer/translations/`, one
file per language with the same keys, which a test holds together. The
console reports in English.

## An inventory is the run

Inventories live in the user's data directory (`~/.local/share/catena/inventory`
on Linux, `~/Library/Application Support/catena/inventory` on macOS,
`%APPDATA%\catena\inventory` on Windows), in `../ansible/inventory/` when run
from a checkout, or under `CATENA_INVENTORY_ROOT`. The page has two tabs.
Inventory lists them, one per line, and creates new ones. Installation is one
page: the target and the configuration in one form, the button that verifies
and installs, the install's output, and then the server access section with
the passwords and the way into the panel. Every field starts from the
registry's default, and a knob that declares an `example` shows it as a
placeholder, never as a value. The button saves the whole form, whatever the
verification finds:

```
<inventory root>/<name>/
  .env                     the answers, written by seed.py's own writer: the
                           file the console reads. NOT the credentials.
  hosts.yml                the server's name.
  .catena-installer.json   the install's state and the installer that runs it. 0600.
```

The provider's password and an admin password pin are held in memory for the
life of the process. The pin reaches the server as a transient 0600 file for
the run, deleted when it ends; the provider's password never leaves this
machine. A reopened inventory asks for them again. That is the honest cost of
not writing them to a client's disk.

What the install prints goes to the console and to the page from memory, never
to a file: it carries the passwords the server shows once. The server prints
them as JSON between two marker lines; the page shows each one in the server
access section, and the console window does not.

## The console, for operators and the bench

```sh
catena-installer install --inventory clientco [-i FILE] [--release vX.Y.Z | --image REF]
                         [--address A] [--tags T] [--reinstalled] [--no-confirm]
                         [--keyset-json] [--ca-file F] [--insecure-http]
```

`-i FILE` installs a new server from one input file: the `.env` keys of the
registry, `host_name` (the server's name), `host_initial_password` (the
provider's password, used once) and an optional `admin_password` pin. Without
it the inventory's `.env` must exist (`init`, then fill it in). On a terminal
the console asks for the provider's password when no key opens the server,
and whether a server presenting a new host key was reinstalled;
`--no-confirm` asks nothing and stops instead. `--image` installs from any
catena-admin image reference, and `--ca-file` / `--insecure-http` reach a
private registry: the test bench serves the image it built that way.

Exit statuses: 0 installed; 1 the install failed; 3 it finished and a check
failed; 4 the server refused the second leg's session.
