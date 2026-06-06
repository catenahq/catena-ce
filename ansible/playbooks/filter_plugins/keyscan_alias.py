"""Ansible filter: rewrite ssh-keyscan output so each entry lists an
extra alias hostname alongside the scanned hostname.

ssh-keyscan emits one entry per line as

    HOST keytype keydata

where paramiko's `HostKeys.load` matches the connection hostname
against the literal first field. From inside a docker container,
ssh_dispatch.py connects to `host.docker.internal` (resolved via
extra_hosts host-gateway in services/catena-admin/dokploy.compose.yml),
so the entry must list that name in addition to whatever
ssh-keyscan saw. known_hosts treats a comma-list of names as
aliases of the same key:

    host.docker.internal,HOST keytype keydata

This filter exists so the role can stay in Python -- the equivalent
inline Jinja regex_replace silently drops the `\\1` backref through
the YAML folded scalar + Jinja string-literal layers, emits a literal
backslash-one, and paramiko's `load_host_keys` propagates
InvalidHostKey on the malformed line (InvalidHostKey is NOT a
subclass of SSHException, so paramiko's own load() doesn't catch it).

Two filters are exposed; both do the same line transform but differ
in return type so callers don't need a Jinja `join('\\n')` (which
collapses through YAML folded-scalar + Jinja string-literal layers
to a literal two-character backslash-n on some Ansible versions):

    keyscan_alias_lines  -> list[str], one rewritten line per entry
    keyscan_alias        -> str, all rewritten lines joined with
                            literal '\\n' and a trailing newline,
                            ready to drop into copy:content=

Comment lines (`#...`) and empty lines pass through verbatim.
"""

from __future__ import annotations


def keyscan_alias_lines(lines, alias):
    """Prefix `alias,` onto the first whitespace-separated field of
    each non-comment ssh-keyscan line. Comment and blank lines pass
    through unchanged. Returns a list of rewritten lines."""
    if lines is None:
        return []
    if isinstance(lines, str):
        lines = lines.splitlines()
    if not isinstance(alias, str) or not alias:
        raise ValueError("keyscan_alias: alias must be a non-empty string")

    out: list[str] = []
    for raw in lines:
        line = raw.rstrip("\n")
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            out.append(line)
            continue
        head, sep, rest = line.partition(" ")
        if not sep:
            head, sep, rest = line.partition("\t")
        if not sep:
            out.append(line)
            continue
        out.append(f"{alias},{head}{sep}{rest}")
    return out


def keyscan_alias(lines, alias):
    """Same transform as `keyscan_alias_lines` but returns one string
    with literal newline separators + a trailing newline, ready for
    ansible.builtin.copy's `content:` field."""
    return "\n".join(keyscan_alias_lines(lines, alias)) + "\n"


class FilterModule:
    def filters(self):
        return {
            "keyscan_alias": keyscan_alias,
            "keyscan_alias_lines": keyscan_alias_lines,
        }
