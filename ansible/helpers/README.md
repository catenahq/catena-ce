# Helpers

Python shared by more than one caller: the roles in
`../bootstrap/roles/` and `../reconcile/roles/`, the install on the
server (`../install-host.sh`), and the installer on the client's machine
(catena-ce `installer/`), which carries `fetch_release.py` and what it
imports to the server before the release is there. Each module's
docstring says what it does.

`onbox_config.py` is the only sanctioned writer of the on-box config
store. The catena-admin payload installs it as
`/usr/local/bin/catena-onbox-config`, with
`../scripts/catena-restic-key.py`: the panel image's dispatch drop-in
runs them, and an image ships every command it authorises. It also puts
`public_https.py` in its lib, where the gated-route verifier imports it.

Unit tests for these live in `../tests/unit/`.
