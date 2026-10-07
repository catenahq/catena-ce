#!/usr/bin/env bash
# Install this server from the catena-ce tree this script belongs to: the tree
# inside a catena-admin release image, fetched onto the server by the
# installer's starter script, which runs this as root. Every step after the
# fetch is this release's own code, so the roles, the engines and the panel
# the server ends up with are one version.
#
# Two legs, each run by the installer through its own SSH session:
#
#   accounts  the ops and panel accounts with the installer's public key, and
#             ops' sudo (playbooks/accounts.yml). The provider's login is left
#             as it is.
#   system    everything else, in a session that logged in as ops with that
#             key: playbooks/bootstrap.yml, show-keyset.yml (the passwords,
#             printed between the KEYSET markers), converge.yml, the recorded
#             version, then validate.yml (vantages 1 and 2). It refuses to start
#             unless helpers/session_proof.py proves the session, because its
#             first role turns off every login but ops.
#
# `uninstall` runs playbooks/uninstall.yml alone.
#
# usage: install-host.sh --leg accounts|system|uninstall --image REF
#          --name NAME --ops-user USER --ops-key FILE --env-file FILE
#          [--adopt-file FILE] [--address A] [--session "$SSH_CONNECTION"]
#          [--tags T] [--keyset-json]
#   --address  where the installer reaches the server, for the panel forward
#              the passwords block names
#
# Exit codes: 0 installed; 1 a step failed; 2 usage; 3 the install finished
# and the on-host checks failed; 4 the session proved nothing.
set -euo pipefail

KEYSET_BEGIN="-----BEGIN CATENA PASSWORDS-----"
KEYSET_END="-----END CATENA PASSWORDS-----"

ansible_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
venv=/opt/catena/ansible

say() { printf 'catena-install: %s\n' "$*" >&2; }
usage() { say "$1"; exit 2; }

leg="" image="" name="" ops_user="" ops_key="" env_file="" adopt_file=""
address="" session="" tags="" keyset_json=false
while [ $# -gt 0 ]; do
    case "$1" in
        --leg) leg="${2:-}"; shift 2 ;;
        --image) image="${2:-}"; shift 2 ;;
        --name) name="${2:-}"; shift 2 ;;
        --ops-user) ops_user="${2:-}"; shift 2 ;;
        --ops-key) ops_key="${2:-}"; shift 2 ;;
        --env-file) env_file="${2:-}"; shift 2 ;;
        --adopt-file) adopt_file="${2:-}"; shift 2 ;;
        --address) address="${2:-}"; shift 2 ;;
        --session) session="${2:-}"; shift 2 ;;
        --tags) tags="${2:-}"; shift 2 ;;
        --keyset-json) keyset_json=true; shift ;;
        *) usage "unknown argument: $1" ;;
    esac
done
case "$leg" in
    accounts|system|uninstall) ;;
    *) usage "--leg must be accounts, system or uninstall" ;;
esac
for required in image name ops_user ops_key env_file; do
    [ -n "${!required}" ] || usage "--${required//_/-} is required"
done
[ "$(id -u)" -eq 0 ] || usage "run as root"

# The inputs arrive in the login account's upload directory. They are copied
# here and the originals removed at once: the adopt file can hold a password.
work="$(mktemp -d /var/lib/catena/install/run.XXXXXX)"
trap 'rm -rf "$work"' EXIT
install -m 0600 "$ops_key" "$work/ops.pub"
install -m 0600 "$env_file" "$work/env"
extra=(-e "@$work/vars.json")
if [ -n "$adopt_file" ]; then
    install -m 0600 "$adopt_file" "$work/adopt.yml"
    rm -f "$adopt_file"
    extra+=(-e "@$work/adopt.yml")
fi

# The ansible-core this release names (bootstrap/roles/ansible_runtime), the
# same venv and pin catena-admin's catena-converge repairs to.
spec="$(python3 - "$ansible_dir/bootstrap/roles/ansible_runtime/defaults/main.yml" <<'PY'
import re, sys
text = open(sys.argv[1], encoding="utf-8").read()
print(re.search(r'^ansible_runtime_spec:\s*"([^"]+)"', text, re.M).group(1))
PY
)"
if [ ! -x "$venv/bin/python3" ]; then
    say "creating the Ansible runtime at $venv"
    python3 -m venv "$venv"
fi
say "installing $spec into $venv"
"$venv/bin/pip" install --quiet --disable-pip-version-check --upgrade "$spec"

# One host, local connection, named as the installer names it; the .env
# beside the inventory is where the dotenv lookup reads the installer's
# answers, as it would from the installer's own inventory.
inventory="$work/inventory"
mkdir -m 0700 "$inventory"
install -m 0600 "$work/env" "$inventory/.env"
cat > "$inventory/hosts.yml" <<YAML
vps:
  hosts:
    $name:
      ansible_connection: local
      ansible_python_interpreter: $venv/bin/python3
      public_ip: "{{ lookup('dotenv', 'HOST_PUBLIC_IP') }}"
YAML

python3 - "$work/ops.pub" "$work/vars.json" "$leg" "$address" <<'PY'
import json, sys
key = open(sys.argv[1], encoding="utf-8").read().strip().splitlines()[0]
doc = {"ops_ssh_public_keys": [key]}
if sys.argv[3] == "system":
    doc["catena_install_key_proven"] = True
if sys.argv[4]:
    doc["catena_ssh_address"] = sys.argv[4]
with open(sys.argv[2], "w", encoding="utf-8") as fh:
    json.dump(doc, fh)
PY
chmod 0600 "$work/vars.json"

export ANSIBLE_CONFIG="$ansible_dir/ansible.cfg"
export CATENA_ADMIN_IMAGE="$image" CATENA_PAYLOAD_IMAGE="$image"
export PYTHONUNBUFFERED=1
# sudo can keep the login's HOME, where Ansible would leave root-owned files.
export HOME=/root
cd "$ansible_dir"

play() {
    say "running playbooks/$1"
    "$venv/bin/ansible-playbook" -i "$inventory" "playbooks/$1" "${@:2}"
}

if [ "$leg" = accounts ]; then
    play accounts.yml "${extra[@]}"
    say "accounts ready: reconnect as $ops_user to continue"
    exit 0
fi
if [ "$leg" = uninstall ]; then
    play uninstall.yml "${extra[@]}"
    exit 0
fi

if ! python3 "$ansible_dir/helpers/session_proof.py" --user "$ops_user" \
        --ssh-connection "$session"; then
    say "refusing to continue: this session did not prove that $ops_user opens with its key"
    exit 4
fi

play bootstrap.yml "${extra[@]}" || exit 1

# The passwords, shown now so they are on screen during the long converge,
# and again at the end. The play writes them into a 0700 directory only this
# run reads, and the trap removes it.
keyset_dir="$work/keyset"
mkdir -m 0700 "$keyset_dir"
keyset=""
if play show-keyset.yml "${extra[@]}" -e "keyset_out=$keyset_dir"; then
    keyset="$(python3 - "$keyset_dir" "$keyset_json" <<'PY'
import json, pathlib, sys
for path in sorted(pathlib.Path(sys.argv[1]).iterdir()):
    doc = json.loads(path.read_text(encoding="utf-8"))
    if sys.argv[2] == "true":
        print(json.dumps({k: v for k, v in doc.items() if k != "banner"}))
    else:
        print(doc["banner"].rstrip("\n"))
PY
)"
    rm -rf "$keyset_dir"
fi
show_keyset() {
    if [ -n "$keyset" ]; then
        printf '%s\n%s\n%s\n' "$KEYSET_BEGIN" "$keyset" "$KEYSET_END"
    else
        say "the passwords could not be shown; running the install again shows them"
    fi
}
show_keyset

converge=()
[ -n "$tags" ] && converge=(--tags "$tags")
play converge.yml "${extra[@]}" "${converge[@]}" || exit 1

# The version this server now runs, read by a later install, a restore onto a
# new server and the panel's update lane. A converge scoped by --tags applied
# part of a version, which is not one to record.
if [ -z "$tags" ]; then
    /usr/local/bin/catena-stack-update-managed pin --image "$image"
fi

validated=0
play validate.yml "${extra[@]}" --tags vantage1,vantage2 || validated=3

say "passwords, as shown after bootstrap:"
show_keyset
exit "$validated"
