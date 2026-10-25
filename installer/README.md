# catena-installer

Installs [Catena](https://catena.run) on your server and opens its admin panel.
Catena turns a fresh server into a place to run your business apps, with
backups, updates, monitoring and secure access built in.

## Install Catena

You need:

- [uv](https://docs.astral.sh/uv/getting-started/installation/) on your computer (Windows, macOS or Linux)
- a fresh x86_64 server running the current Debian stable release, with 4 GB of
  memory or more, and its SSH login
- your admin email address

Then run:

```sh
uvx catena-installer
```

A page opens in your browser. Enter how to reach the server and your admin
details, then start the install. When it ends, save the passwords it shows in
your password manager and open the panel.

The [installation guide](https://docs.catena.run/en/installation/) covers the
requirements, the first sign-in and what to do when the installer reports a
problem.

## How it works

The installer runs on your computer and connects to your server over SSH. The
server downloads the Catena release and installs itself, and the installer
shows its progress. When the installer needs the provider's password, it uses
it once for the first login and never saves it.

## From a terminal

Each server's settings live on your computer under a name you choose:

```sh
uvx catena-installer init --inventory prod      # create the settings file to fill in
uvx catena-installer install --inventory prod   # install, or repair an installed server
uvx catena-installer connect --inventory prod   # open the admin panel
```

## Links

- Website: <https://catena.run>
- Documentation: <https://docs.catena.run>
- Source code: <https://github.com/catenahq/catena-ce>, the installer in
  [`installer/`](https://github.com/catenahq/catena-ce/tree/HEAD/installer)
  ([how it works inside](https://github.com/catenahq/catena-ce/blob/HEAD/installer/catena_installer/README.md))
