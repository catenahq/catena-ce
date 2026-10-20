#!/usr/bin/env bash
# Start a Catena install on this server, as root: make sure python3 is here,
# fetch the catena-admin release image's catena-ce tree and payload with
# fetch_release.py (sent beside this script), and hand over to that tree's
# ansible/install-host.sh. The installer sends both files through its own SSH
# session. Nothing here depends on the release, so one installer can start
# the install of any release: every version-specific step is the fetched
# tree's own.
#
# An install of this server runs one at a time: a leg takes
# /run/catena-install.lock before it fetches and install-host.sh keeps it to
# its end, so a rerun after a dropped connection waits for the leg still
# running and never removes the tree it runs from.
#
# usage: starter.sh [--no-fetch] [fetch_release.py arguments] -- <install-host.sh arguments>
#   --no-fetch  run the tree an earlier leg of this install fetched
set -euo pipefail

stage_root=/var/lib/catena/install
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

say() { printf 'catena-install: %s\n' "$*" >&2; }

[ "$(id -u)" -eq 0 ] || { say "run as root"; exit 2; }
if ! grep -qx 'ID=debian' /etc/os-release 2>/dev/null; then
    say "this server is not Debian; Catena installs on Debian 12 or 13"
    exit 2
fi

fetch=true
fetch_args=()
while [ $# -gt 0 ]; do
    case "$1" in
        --no-fetch) fetch=false; shift ;;
        --) shift; break ;;
        *) fetch_args+=("$1"); shift ;;
    esac
done

exec 8>>/run/catena-install.lock
if ! flock -n 8; then
    say "waiting for an earlier install on this server to end"
    flock -w 3600 8 || { say "an earlier install held /run/catena-install.lock for an hour"; exit 1; }
fi

missing=()
for pkg in python3 python3-venv sudo ca-certificates; do
    dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q 'install ok installed' \
        || missing+=("$pkg")
done
if [ ${#missing[@]} -gt 0 ]; then
    say "installing ${missing[*]}"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq "${missing[@]}"
fi

install -d -m 0700 "$stage_root"
if $fetch; then
    dest="$stage_root/$(date -u +%Y%m%dT%H%M%SZ)"
    say "fetching the release into $dest"
    python3 "$here/fetch_release.py" --dest "$dest" "${fetch_args[@]}"
    ln -sfn "$dest" "$stage_root/current"
    for old in "$stage_root"/2*; do
        [ "$old" = "$dest" ] || rm -rf "$old"
    done
fi

current="$(readlink -f "$stage_root/current")"
[ -f "$current/IMAGE" ] || { say "no fetched release under $stage_root"; exit 1; }
exec bash "$current/usr/local/share/catena-ce/ansible/install-host.sh" \
    --image "$(cat "$current/IMAGE")" "$@"
