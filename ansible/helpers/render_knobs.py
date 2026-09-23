#!/usr/bin/env python3
"""Render helpers/knobs.yml to helpers/knobs.json, and check the two agree.

THE JSON IS THE ARTIFACT EVERY CONSUMER READS. The YAML is what a human edits:
it carries the rationale, and the rationale is the half that decides whether
the next knob is declared in the right place.

Why a rendered copy rather than one file. `onbox_config.py` runs as root on a
minimal target host whose system python has no PyYAML -- it says so about
itself, and being stdlib-only is the reason it can run there at all. So the
registry has to reach that host as JSON. catena-admin reads the same JSON
rather than regex-parsing python out of a sibling repo, which is what it did
before this file existed.

    render_knobs.py --write     regenerate knobs.json
    render_knobs.py --check     exit 1 when it is stale

The check runs in CI. A knob added to the YAML without re-rendering would be a
knob the panel and the store never learn about, which is exactly the drift the
registry exists to stop -- so it has to fail loudly rather than take effect on
whoever next happens to run --write.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "knobs.yml"
RENDERED = HERE / "knobs.json"

RESIDENCES = ("store", "controller", "host")
KINDS = ("secret", "text", "choice")
SECTIONS = ("secrets", "config")
# The page's fieldsets. Not validated against catena-admin's Group constants --
# this repo cannot import Go -- so the panel's own schema test is what holds
# the two lists together.
GROUPS = ("tunnel", "backup", "mail", "alerts", "share", "access", "license",
          "hostnames")


class KnobError(ValueError):
    """The registry is malformed. Raised with the key at fault named."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise KnobError(message)


def _check_panel(key: str, panel: dict) -> None:
    _require(isinstance(panel, dict), f"{key}: panel is not a mapping")
    for required in ("group", "section", "kind", "optional"):
        _require(required in panel, f"{key}: panel is missing {required}")
    _require(panel["group"] in GROUPS,
             f"{key}: panel group {panel['group']!r} is not one of {GROUPS}")
    _require(panel["section"] in SECTIONS,
             f"{key}: panel section {panel['section']!r} is not one of {SECTIONS}")
    _require(panel["kind"] in KINDS,
             f"{key}: panel kind {panel['kind']!r} is not one of {KINDS}")
    _require(isinstance(panel["optional"], bool),
             f"{key}: panel optional is not a boolean")
    options = panel.get("options")
    if panel["kind"] == "choice":
        # A choice with no options renders an empty select, which is a control
        # that cannot be used and does not look broken.
        _require(isinstance(options, list) and options,
                 f"{key}: a choice field needs a non-empty options list")
        _require(all(isinstance(o, str) for o in options),
                 f"{key}: choice options must be strings")
    else:
        _require(options is None,
                 f"{key}: options on a {panel['kind']} field are read by nothing")


def load(source: Path = SOURCE) -> dict:
    """Parse and VALIDATE the registry. Every consumer reads the rendered JSON,
    so this is the only place the shape is enforced."""
    doc = yaml.safe_load(source.read_text())
    _require(isinstance(doc, dict), "knobs.yml: top level is not a mapping")
    _require(doc.get("version") == 1, "knobs.yml: expected version 1")

    secrets = doc.get("secrets") or []
    config = doc.get("config") or []
    _require(isinstance(secrets, list), "knobs.yml: secrets is not a list")
    _require(isinstance(config, list), "knobs.yml: config is not a list")

    seen: set[str] = set()
    for entry in [*secrets, *config]:
        _require(isinstance(entry, dict), f"knobs.yml: entry {entry!r} is not a mapping")
        key = entry.get("key")
        _require(isinstance(key, str) and key, f"knobs.yml: entry with no key: {entry!r}")
        _require(key not in seen, f"{key}: declared twice")
        seen.add(key)
        if "panel" in entry:
            _check_panel(key, entry["panel"])

    for entry in secrets:
        key = entry["key"]
        _require("residence" not in entry,
                 f"{key}: a secret always lives in the store; residence is not its to state")
        _require("env" not in entry,
                 f"{key}: a secret never appears in the .env template")
        panel = entry.get("panel")
        if panel:
            _require(panel["section"] == "secrets",
                     f"{key}: a secret renders in the secrets section")
            _require(panel["kind"] == "secret",
                     f"{key}: a secret renders as a secret field")

    for entry in config:
        key = entry["key"]
        residence = entry.get("residence")
        _require(residence in RESIDENCES,
                 f"{key}: residence {residence!r} is not one of {RESIDENCES}")
        if "var" in entry:
            _require(residence == "store",
                     f"{key}: only a stored value is projected onto an Ansible fact")
        if entry.get("panel"):
            _require(residence == "store",
                     f"{key}: the panel writes the store, so only a stored value "
                     "can be a field on it")
            _require(entry["panel"]["section"] == "config",
                     f"{key}: a config value renders in the config section")

    # `depends` is checked last, against the whole registry: it names another
    # knob and values of it, and both halves have to resolve or the page hides
    # a field on a condition that can never be true.
    choices = {
        e["key"]: e["panel"]["options"]
        for e in [*secrets, *config]
        if e.get("panel", {}).get("kind") == "choice"
    }
    for entry in [*secrets, *config]:
        depends = entry.get("depends")
        if depends is None:
            continue
        key = entry["key"]
        _require(isinstance(depends, dict) and "choice" in depends and "values" in depends,
                 f"{key}: depends needs both `choice` and `values`")
        governor = depends["choice"]
        _require(governor in choices,
                 f"{key}: depends on {governor!r}, which is not a choice field")
        unknown = [v for v in depends["values"] if v not in choices[governor]]
        _require(not unknown,
                 f"{key}: depends on {governor} values {unknown} that it does not offer")
        _require(entry.get("panel"),
                 f"{key}: depends governs whether the PANEL shows a field, and "
                 "this knob is not one")

    return doc


def render(doc: dict) -> str:
    """The artifact. Declaration order is preserved -- it is the order the
    settings page renders fields within a group -- and the trailing newline
    keeps the file diffable."""
    return json.dumps(doc, indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="regenerate knobs.json")
    mode.add_argument("--check", action="store_true",
                      help="exit 1 when knobs.json is stale")
    args = ap.parse_args(argv)

    try:
        payload = render(load())
    except KnobError as exc:
        print(f"knobs.yml: {exc}", file=sys.stderr)
        return 1

    if args.write:
        RENDERED.write_text(payload)
        print(f"wrote {RENDERED}")
        return 0

    current = RENDERED.read_text() if RENDERED.exists() else ""
    if current == payload:
        print(f"{RENDERED.name} is current")
        return 0
    print(f"{RENDERED.name} is STALE -- run `python3 helpers/render_knobs.py --write` "
          "and commit the result", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
