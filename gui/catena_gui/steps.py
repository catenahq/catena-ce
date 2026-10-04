"""What each section asks, and what it proves before the install starts.

The FIELDS are the registry's; only the PROBES, and the one field the install
contract takes beside the registry (the provider's password), are here. Adding
a question is a registry edit, and adding a proof is a function here.

Install runs every probe first. An installer that collects every answer and
discovers at the end that the first one was wrong has spent a client's whole
sitting to tell them something it knew at the start. And a step passes only
when the thing it is about has been observed working: a step that cannot
observe says so rather than assuming.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from dataclasses import dataclass, field as _dc_field
from pathlib import Path

from . import i18n, registry

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
    """One question in a section, resolved from the registry. `label` and
    `help` are by language."""

    key: str
    label: dict[str, str]
    secret: bool
    optional: bool
    options: list[str]
    default: str
    help: dict[str, str]
    example: str = ""
    suggestions: list[str] = _dc_field(default_factory=list)


@dataclass
class Step:
    """One section of the page; `title`, `doc` and `note` are by language."""

    name: str
    title: dict[str, str]
    doc: dict[str, str]
    note: dict[str, str]
    fields: list[Field]


def _field(doc: dict, entry: dict) -> Field:
    return Field(
        key=entry["key"],
        label=entry["label"],
        secret=registry.is_secret(doc, entry["key"]),
        optional=not registry.is_required(entry),
        options=registry.options_for(entry),
        default=registry.default_for(entry),
        help=entry["help"],
        example=registry.example_for(entry),
        suggestions=registry.suggestions_for(entry),
    )


def _provider_password() -> Field:
    return Field(
        key=PROVIDER_PASSWORD, label=i18n.every("provider_password.label"),
        secret=True, optional=True, options=[], default="",
        help=i18n.every("provider_password.help"))


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
                        note=step.get("note") or {}, fields=fields))
    return out


def missing_required(step: Step, values: dict[str, str],
                     lang: str = i18n.DEFAULT) -> list[Check]:
    """A blocking line for each required field left empty. Shown before the
    step's own probes, which have nothing to observe without it."""
    return [Check(i18n.text(lang, "check.required", label=f.label[lang]), False,
                  i18n.text(lang, "check.fill_in"))
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


def check_target(answers: dict[str, str], secrets: dict[str, str],
                 lang: str = i18n.DEFAULT) -> list[Check]:
    """An SSH server answers, and the install can log in.

    Either the key already opens the server -- the provider installed it when
    the server was ordered, or a previous install did, in which case root is
    refused by then and the ops account takes it -- or the provider's password
    opens the initial login and the install adds the key with it.
    """
    def t(key: str, **values: object) -> str:
        return i18n.text(lang, key, **values)

    host = (answers.get("HOST_PUBLIC_IP") or "").strip()
    port_raw = (answers.get("HOST_SSH_PORT") or "22").strip() or "22"
    if not host or not port_raw.isdigit():
        return [] if not host else [Check(t("check.port"), False,
                                          t("check.port_invalid", port=repr(port_raw)))]
    port = int(port_raw)
    banner = _ssh_banner(host, port)
    checks = [Check(t("check.ssh_answers", host=host, port=port),
                    banner.startswith("SSH-"), banner or t("check.ssh_silent"))]
    raw = (answers.get("SSH_PRIVATE_KEY") or "").strip()
    key = os.path.expanduser(raw) if raw else ""
    have_key = bool(key) and Path(key).is_file() and Path(key + ".pub").is_file()
    checks.append(Check(t("check.keypair"), have_key,
                        "" if have_key else
                        t("check.keypair_missing", path=raw or t("check.no_path"))))
    if not (checks[0].ok and have_key):
        return checks
    initial = (answers.get("HOST_INITIAL_USER") or "root").strip() or "root"
    tried = []
    for user in dict.fromkeys((initial, (answers.get("OPS_USER") or "ops").strip())):
        ok, why = _ssh_login(host, port, user, key)
        if ok:
            checks.append(Check(t("check.key_opens", login=f"{user}@{host}"), True,
                                t("check.no_password_needed")))
            return checks
        tried.append(f"{user}: {why or t('check.refused')}")
    password = secrets.get(PROVIDER_PASSWORD) or ""
    if not password:
        checks.append(Check(t("check.key_refused"), False,
                            t("check.key_refused_detail", tried="; ".join(tried),
                              user=initial)))
        return checks
    ok, why = _password_login(host, port, initial, password)
    checks.append(Check(t("check.password_opens", login=f"{initial}@{host}"), ok,
                        t("check.password_adds_key") if ok
                        else why or t("check.password_refused")))
    return checks


# The probe for each step that has one, by name.
PROBES = {
    "target": check_target,
}


def validate(step: str, answers: dict[str, str], secrets: dict[str, str],
             lang: str = i18n.DEFAULT) -> list[Check]:
    probe = PROBES.get(step)
    if probe is None:
        return []
    return probe(answers, secrets, lang)


def blocked(checks: list[Check]) -> bool:
    return any(check.blocks for check in checks)


# --- the way in -------------------------------------------------------------


def forward_command(answers: dict[str, str]) -> str:
    """The SSH command that reaches the panel and Portainer once the install
    ends: both answer the server's loopback only, and the panel account can do
    nothing but forward."""
    host = (answers.get("HOST_PUBLIC_IP") or "").strip() or "<server-address>"
    port = (answers.get("HOST_SSH_PORT") or "22").strip() or "22"
    key = (answers.get("SSH_PRIVATE_KEY") or "").strip()
    opts = f" -i {key}" if key else ""
    opts += f" -p {port}" if port != "22" else ""
    return (f"ssh -N -L {PANEL_PORT}:127.0.0.1:{PANEL_PORT} "
            f"-L {PORTAINER_PORT}:127.0.0.1:{PORTAINER_PORT}{opts} "
            f"{PANEL_USER}@{host}")
