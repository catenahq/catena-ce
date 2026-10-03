"""What each section asks, and what it proves before the install starts.

THE INSTALLER ASKS ONLY FOR WHAT REACHES AND INSTALLS THE SERVER: its address,
the key that opens it, and the administrator's email. The domain, the private
network and the backups are entered in the panel once the server runs, so no
section here asks for them.

A SECTION WITH A CHECK PROVES SOMETHING REAL, and Install runs every check
first. An installer that collects every answer and discovers at the end that
the first one was wrong has spent a client's whole sitting to tell them
something it knew at the start.

AND IT NEVER REPORTS SUCCESS FOR A HOST THAT DOES NOT WORK. A step passes only
when the thing it is about has been observed working, and a step that cannot
observe says so rather than assuming.

The FIELDS are the registry's; only the PROBES, and the one field the install
contract takes beside the registry (the provider's password), are here. That
split is what keeps the launcher free of knob knowledge: adding a question is a
registry edit, and adding a proof is a function here.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from dataclasses import dataclass, field as _dc_field
from pathlib import Path

from . import registry

# The host-only UIs and the account that forwards to them. Product constants
# (catena-ce playbooks/group_vars/all/main.yml), printed in the access section.
PANEL_PORT = 9010
PORTAINER_PORT = 9000
PANEL_USER = "panel"

# The install contract's name for the provider's password (seed files a
# `host_`-prefixed key under the host). Not a knob: nothing on the server keeps
# it, and the launcher does not either.
PROVIDER_PASSWORD = "host_initial_password"


@dataclass
class Check:
    """One thing a step proved, or could not.

    `blocking` separates "this is wrong" from "this could not be established
    from here". An absence of evidence is not evidence of a fault, and a step
    that treated the two alike would either refuse valid installs or pass
    broken ones -- there is no setting of one flag that avoids both.
    """

    label: str
    ok: bool
    detail: str = ""
    blocking: bool = True

    @property
    def blocks(self) -> bool:
        return not self.ok and self.blocking


@dataclass
class Field:
    """One question in a section, resolved from the registry."""

    key: str
    label: str
    secret: bool
    optional: bool
    options: list[str]
    default: str
    doc: str
    example: str = ""
    suggestions: list[str] = _dc_field(default_factory=list)


@dataclass
class Step:
    name: str
    title: str
    doc: str
    note: str
    validates: str
    fields: list[Field]


def _field(doc: dict, entry: dict) -> Field:
    return Field(
        key=entry["key"],
        label=str(entry["label"]),
        secret=registry.is_secret(doc, entry["key"]),
        optional=not registry.is_required(entry),
        options=registry.options_for(entry),
        default=registry.default_for(entry),
        doc=str(entry.get("doc") or ""),
        example=registry.example_for(entry),
        suggestions=registry.suggestions_for(entry),
    )


def _provider_password() -> Field:
    return Field(
        key=PROVIDER_PASSWORD, label="Host initial user's password", secret=True,
        optional=True, options=[], default="",
        doc=("The password the provider gave for the initial login. Needed only "
             "when the server does not accept the SSH key yet: the install uses "
             "it once, to add the key, and nothing keeps it."))


def build(doc: dict) -> list[Step]:
    """The whole page, from the registry, with the provider's password in the
    section that reaches the server."""
    out = []
    for step in registry.steps(doc):
        fields = [_field(doc, entry)
                  for entry in registry.step_fields(doc, step["name"])]
        if step["name"] == "target":
            fields.append(_provider_password())
        out.append(Step(name=step["name"], title=step["title"], doc=step["doc"],
                        note=str(step.get("note") or ""),
                        validates=str(step.get("validates") or ""), fields=fields))
    return out


def missing_required(step: Step, values: dict[str, str]) -> list[Check]:
    """A blocking line for each required field left empty. Shown before the
    step's own probes, which have nothing to observe without it."""
    return [Check(f"{f.label} is required", False, "fill it in")
            for f in step.fields
            if not f.optional and not (values.get(f.key) or "").strip()]


# --- the probes --------------------------------------------------------------


def _ssh_banner(host: str, port: int, timeout: float = 6.0) -> str:
    """The first line the port sends, or "" when nothing answered. An SSH
    server speaks first, with `SSH-2.0-...`; anything else on the port is not
    one."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            return sock.recv(256).decode("ascii", "replace").strip()
    except OSError:
        return ""


def _ssh_login(host: str, port: int, user: str, key: str) -> tuple[bool, str]:
    """Whether `user` logs in with `key` alone. BatchMode: no password prompt,
    no host-key prompt. The host key is not recorded -- this is a probe, and a
    probe that edits the client's known_hosts leaves something behind."""
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
            "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR", "-i", key, "-p", str(port), f"{user}@{host}", "true"]
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=25)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    return out.returncode == 0, (out.stderr or "").strip()[-200:]


def _password_login(host: str, port: int, user: str, password: str) -> tuple[bool, str]:
    """Whether the provider's password opens `user`, asked the way the install
    will use it: helpers/install_key.py, in its check-only mode, which logs in
    and changes nothing. The password rides the environment, never the argv."""
    script = registry.ANSIBLE_DIR / "helpers" / "install_key.py"
    argv = [sys.executable, str(script), host, f"--user={user}", f"--port={port}",
            "--check-password"]
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=90,
                             env={**os.environ, "INSTALL_KEY_PASSWORD": password})
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    said = ((out.stdout or "") + (out.stderr or "")).strip().splitlines()
    return out.returncode == 0, (said[-1] if said else "")[-200:]


def check_target(answers: dict[str, str], secrets: dict[str, str]) -> list[Check]:
    """An SSH server answers, and the install can log in.

    Either the key already opens the server -- the provider installed it when
    the server was ordered, or a previous run did, in which case root is
    refused by now and the ops account takes it -- or the provider's password
    opens the initial login and the install adds the key with it.
    """
    host = (answers.get("HOST_PUBLIC_IP") or "").strip()
    port_raw = (answers.get("HOST_SSH_PORT") or "22").strip() or "22"
    if not host or not port_raw.isdigit():
        return [] if not host else [Check("the SSH port", False,
                                          f"{port_raw!r} is not a port number")]
    port = int(port_raw)
    banner = _ssh_banner(host, port)
    checks = [Check(f"an SSH server answers at {host}:{port}",
                    banner.startswith("SSH-"),
                    banner or "nothing accepted a connection. A server still "
                    "being delivered is the ordinary reason, and this section is "
                    "where a run waits for it")]
    raw = (answers.get("SSH_PRIVATE_KEY") or "").strip()
    key = os.path.expanduser(raw) if raw else ""
    have_key = bool(key) and Path(key).is_file() and Path(key + ".pub").is_file()
    checks.append(Check("the keypair is on this machine", have_key,
                        "" if have_key else f"{raw or '(no path)'} or its .pub is "
                        "missing; generate one with ssh-keygen -t ed25519 -f "
                        "~/.ssh/catena_ed25519"))
    if not (checks[0].ok and have_key):
        return checks
    initial = (answers.get("HOST_INITIAL_USER") or "root").strip() or "root"
    tried = []
    for user in dict.fromkeys((initial, (answers.get("OPS_USER") or "ops").strip())):
        ok, why = _ssh_login(host, port, user, key)
        if ok:
            checks.append(Check(f"the key already opens {user}@{host}", True,
                                "no password is needed"))
            return checks
        tried.append(f"{user}: {why or 'refused'}")
    password = secrets.get(PROVIDER_PASSWORD) or ""
    if not password:
        checks.append(Check(
            "the server does not accept this key yet", False,
            "; ".join(tried) + f". Enter the provider's password for {initial}, "
            "and the install adds the key with it, or give the public key to "
            "the provider and check again"))
        return checks
    ok, why = _password_login(host, port, initial, password)
    checks.append(Check(f"the provider's password opens {initial}@{host}", ok,
                        "the install adds the key with it" if ok
                        else why or "the password was refused"))
    return checks


# The probe for each step that has one, by name.
PROBES = {
    "target": check_target,
}


def validate(step: str, answers: dict[str, str], secrets: dict[str, str]) -> list[Check]:
    probe = PROBES.get(step)
    if probe is None:
        return []
    return probe(answers, secrets)


def blocked(checks: list[Check]) -> bool:
    return any(check.blocks for check in checks)


# --- the way in -------------------------------------------------------------


def forward_command(answers: dict[str, str]) -> str:
    """The SSH command that reaches the panel and Portainer once the install
    ends.

    Both answer the server's loopback only, and a forward as the panel account
    -- which can do nothing but forward -- lands there. The domain and the
    private network, entered in the panel afterwards, add their own ways in;
    this one stays.
    """
    host = (answers.get("HOST_PUBLIC_IP") or "").strip() or "<server-address>"
    port = (answers.get("HOST_SSH_PORT") or "22").strip() or "22"
    key = (answers.get("SSH_PRIVATE_KEY") or "").strip()
    opts = f" -i {key}" if key else ""
    opts += f" -p {port}" if port != "22" else ""
    return (f"ssh -N -L {PANEL_PORT}:127.0.0.1:{PANEL_PORT} "
            f"-L {PORTAINER_PORT}:127.0.0.1:{PORTAINER_PORT}{opts} "
            f"{PANEL_USER}@{host}")
