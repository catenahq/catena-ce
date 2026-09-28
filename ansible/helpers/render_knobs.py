#!/usr/bin/env python3
"""Render helpers/knobs.yml to its two artifacts, and check they are current.

THE ARTIFACTS ARE WHAT EVERY CONSUMER READS. The YAML is what a human edits: it
carries the rationale, and the rationale is the half that decides whether the
next knob is declared in the right place.

    helpers/knobs.json                  the registry, for the store, the panel
                                        and the installer
    inventory/example/.env.example      the template a client fills in

Why a rendered copy rather than one file. `onbox_config.py` runs as root on a
minimal target host whose system python has no PyYAML -- it says so about
itself, and being stdlib-only is the reason it can run there at all. So the
registry has to reach that host as JSON. catena-admin reads the same JSON
rather than regex-parsing python out of a sibling repo, which is what it did
before this file existed. The `.env` template is rendered for a different
reason: it was a fourth place the same keys, defaults and prose were written
down by hand.

    render_knobs.py --write     regenerate both artifacts
    render_knobs.py --check     exit 1 when either is stale

`tests/unit/test_knobs_registry_is_the_declaration.py` runs the check, so CI
fails on a stale artifact. A knob added to the YAML without re-rendering would
be a knob the panel and the store never learn about, which is exactly the drift
the registry exists to stop -- so it has to fail loudly rather than take effect
on whoever next happens to run --write.
"""
from __future__ import annotations

import argparse
import json
import sys
import textwrap
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "knobs.yml"
RENDERED = HERE / "knobs.json"
ENV_TEMPLATE = HERE.parent / "inventory" / "example" / ".env.example"

RESIDENCES = ("store", "controller", "host")
KINDS = ("secret", "text", "choice")
SECTIONS = ("secrets", "config")
# The page's fieldsets. Not validated against catena-admin's Group constants --
# this repo cannot import Go -- so the panel's own schema test is what holds
# the two lists together.
GROUPS = ("tunnel", "backup", "mail", "alerts", "share", "access", "license",
          "hostnames")

# The rendered template's comment width, and the characters a `.env` value
# cannot carry unquoted. A default holding one of them would render a line that
# parses back as something else, so it is refused at the source rather than
# quoted on the way out: a template default is an illustration, and one that
# needs escaping is the wrong illustration.
ENV_WIDTH = 74
ENV_UNQUOTABLE = " \t#\"'$"


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


def _check_env(key: str, env: dict, sections: set[str]) -> None:
    _require(isinstance(env, dict), f"{key}: env is not a mapping")
    _require(env.get("section") in sections,
             f"{key}: env section {env.get('section')!r} is not a declared "
             f"section; the template would have nowhere to print it")
    default = env.get("default")
    _require(isinstance(default, str),
             f"{key}: env needs a string default (an empty one marks it optional)")
    _require(not any(c in default for c in ENV_UNQUOTABLE),
             f"{key}: the default {default!r} cannot be written unquoted in a "
             ".env, so it is the wrong default to illustrate the field with")
    options = env.get("options")
    if options is not None:
        _require(isinstance(options, list) and options,
                 f"{key}: env options is empty")
        _require(all(isinstance(o, str) for o in options),
                 f"{key}: env options must be strings")
        _require(default in options,
                 f"{key}: the default {default!r} is not one of {options}")


def load(source: Path = SOURCE) -> dict:
    """Parse and VALIDATE the registry. Every consumer reads a rendered
    artifact, so this is the only place the shape is enforced."""
    doc = yaml.safe_load(source.read_text())
    _require(isinstance(doc, dict), "knobs.yml: top level is not a mapping")
    _require(doc.get("version") == 1, "knobs.yml: expected version 1")

    secrets = doc.get("secrets") or []
    config = doc.get("config") or []
    _require(isinstance(secrets, list), "knobs.yml: secrets is not a list")
    _require(isinstance(config, list), "knobs.yml: config is not a list")

    _require(isinstance(doc.get("env_header"), str) and doc["env_header"].strip(),
             "knobs.yml: env_header is the template's preamble and cannot be empty")
    env_sections = doc.get("env_sections") or []
    _require(isinstance(env_sections, list) and env_sections,
             "knobs.yml: env_sections is the template's running order")
    section_names: list[str] = []
    for section in env_sections:
        _require(isinstance(section, dict), f"env_sections: {section!r} is not a mapping")
        name, title = section.get("name"), section.get("title")
        _require(isinstance(name, str) and name, f"env_sections: {section!r} has no name")
        _require(isinstance(title, str) and title, f"env_sections {name}: no title")
        _require(name not in section_names, f"env_sections {name}: declared twice")
        section_names.append(name)

    gui_steps = doc.get("gui_steps") or []
    _require(isinstance(gui_steps, list) and gui_steps,
             "knobs.yml: gui_steps is the launcher's running order")
    step_names: list[str] = []
    for step in gui_steps:
        _require(isinstance(step, dict), f"gui_steps: {step!r} is not a mapping")
        name = step.get("name")
        _require(isinstance(name, str) and name, f"gui_steps: {step!r} has no name")
        _require(name not in step_names, f"gui_steps {name}: declared twice")
        for field in ("title", "doc", "validates"):
            value = step.get(field)
            _require(isinstance(value, str) and value.strip(),
                     f"gui_steps {name}: no {field}")
        step_names.append(name)

    seen: set[str] = set()
    for entry in [*secrets, *config]:
        _require(isinstance(entry, dict), f"knobs.yml: entry {entry!r} is not a mapping")
        key = entry.get("key")
        _require(isinstance(key, str) and key, f"knobs.yml: entry with no key: {entry!r}")
        _require(key not in seen, f"{key}: declared twice")
        seen.add(key)
        if "panel" in entry:
            _check_panel(key, entry["panel"])
        if "step" in entry:
            _require(entry["step"] in step_names,
                     f"{key}: step {entry['step']!r} is not a declared gui step, "
                     f"so the launcher would have nowhere to ask for it")

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
        if "env" in entry:
            _check_env(key, entry["env"], set(section_names))
        if entry.get("panel"):
            _require(residence == "store",
                     f"{key}: the panel writes the store, so only a stored value "
                     "can be a field on it")
            _require(entry["panel"]["section"] == "config",
                     f"{key}: a config value renders in the config section")

    # A heading with nothing under it renders as a section break followed by the
    # next section, which reads as a key having gone missing.
    used = {e["env"]["section"] for e in config if "env" in e}
    empty = [n for n in section_names if n not in used]
    _require(not empty, f"env_sections: {empty} carry no key")

    # A step with no field is not the same mistake. `keyset` deliberately has
    # none -- it is an acknowledgement, not a form -- so only a step that is
    # neither used nor LAST is a heading nobody filled in.
    asked = {e["step"] for e in [*secrets, *config] if "step" in e}
    orphan = [n for n in step_names[:-1] if n not in asked]
    _require(not orphan,
             f"gui_steps: {orphan} ask for nothing and are not the final step")

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


def rendered(path: Path = RENDERED) -> dict:
    """The registry as the consumers read it. Every reader that has PyYAML
    still reads the JSON, so a stale render makes them all stale together
    rather than making one of them disagree with the rest."""
    return json.loads(path.read_text())


def render(doc: dict) -> str:
    """The registry artifact. Declaration order is preserved -- it is the order
    the settings page renders fields within a group -- and the trailing newline
    keeps the file diffable."""
    return json.dumps(doc, indent=2) + "\n"


def step_knobs(doc: dict, step: str) -> list[dict]:
    """What one installer page asks for, secrets first.

    Secrets first because that is the order a page reads in: the credential
    that proves a thing, then the values it configures. It is also the order
    the settings page uses, so a client who has seen one recognises the other.
    """
    return [entry for entry in [*doc["secrets"], *doc["config"]]
            if entry.get("step") == step]


def env_knobs(doc: dict) -> list[dict]:
    """The config knobs the `.env` carries, in the order the template prints
    them. seed prompts in this order too, so a client answering the prompts and
    a client editing the file walk the same sequence."""
    by_section: dict[str, list[dict]] = {}
    for entry in doc["config"]:
        env = entry.get("env")
        if env:
            by_section.setdefault(env["section"], []).append(entry)
    out: list[dict] = []
    for section in doc["env_sections"]:
        out.extend(by_section.get(section["name"], []))
    return out


def _comment(text: str) -> list[str]:
    """Prose as `#` comment lines, re-wrapped to one column.

    Consecutive unindented lines are one paragraph, so a folded scalar and a
    hand-wrapped literal block come out the same width. A blank line ends a
    paragraph and a line indented further than the margin is held verbatim,
    which is how a command keeps its shape.
    """
    out: list[str] = []
    paragraph: list[str] = []

    def flush() -> None:
        if paragraph:
            # Neither a hyphenated word nor a long path is a wrap point: both
            # break into halves that read as two things.
            out.extend(f"# {chunk}" for chunk
                       in textwrap.wrap(" ".join(paragraph), ENV_WIDTH,
                                        break_on_hyphens=False,
                                        break_long_words=False))
            paragraph.clear()

    for line in (text or "").strip("\n").splitlines():
        if not line.strip():
            flush()
            out.append("#")
        elif line.startswith((" ", "\t")):
            flush()
            out.append(f"# {line}".rstrip())
        else:
            paragraph.append(line.strip())
    flush()
    return out


def render_env(doc: dict) -> str:
    """The `.env` template: the preamble, then one block per section, then one
    commented, defaulted key per knob that declares an env home."""
    lines = _comment(doc["env_header"])
    by_section: dict[str, list[dict]] = {}
    for entry in doc["config"]:
        env = entry.get("env")
        if env:
            by_section.setdefault(env["section"], []).append(entry)

    for section in doc["env_sections"]:
        lines.append("")
        lines.append(f"# --- {section['title']} ".ljust(78, "-"))
        if section.get("doc"):
            lines.append("#")
            lines.extend(_comment(section["doc"]))
        for entry in by_section.get(section["name"], []):
            lines.append("")
            if entry.get("doc"):
                lines.extend(_comment(entry["doc"]))
            options = entry["env"].get("options")
            if options:
                lines.extend(_comment(f"One of: {', '.join(options)}."))
            lines.append(f"{entry['key']}={entry['env']['default']}")
    return "\n".join(lines) + "\n"


# Each artifact: where it lives, and what rendering the source produces for it.
_ARTIFACTS = ((RENDERED, render), (ENV_TEMPLATE, render_env))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="regenerate both artifacts")
    mode.add_argument("--check", action="store_true",
                      help="exit 1 when either artifact is stale")
    args = ap.parse_args(argv)

    try:
        doc = load()
        payloads = [(path, renderer(doc)) for path, renderer in _ARTIFACTS]
    except KnobError as exc:
        print(f"knobs.yml: {exc}", file=sys.stderr)
        return 1

    if args.write:
        for path, payload in payloads:
            path.write_text(payload)
            print(f"wrote {path}")
        return 0

    stale = [path for path, payload in payloads
             if (path.read_text() if path.exists() else "") != payload]
    if not stale:
        print(f"{', '.join(p.name for p in (RENDERED, ENV_TEMPLATE))} are current")
        return 0
    print(f"STALE: {', '.join(str(p) for p in stale)} -- run "
          "`python3 helpers/render_knobs.py --write` and commit the result",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
