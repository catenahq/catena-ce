"""Load and validate helpers/knobs.yml, the knob registry, and render an
inventory `.env` from it.

Every reader with PyYAML calls load(). A host's python has none, so the
converge stages the registry there as JSON
(playbooks/tasks/stage_knob_registry.yml) and the catena-admin image build
does the same for its payload.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import yaml

SOURCE = Path(__file__).resolve().parent / "knobs.yml"

RESIDENCES = ("store", "controller", "host")
KINDS = ("secret", "text", "choice")
SECTIONS = ("secrets", "config")
# The page's fieldsets. Not validated against catena-admin's Group constants --
# this repo cannot import Go -- so the panel's own schema test is what holds
# the two lists together.
GROUPS = ("tunnel", "backup", "mail", "mailserver", "alerts", "share", "access",
          "license", "hostnames", "subdomains", "server")
# The named sources the panel fills a field's choices from at render time,
# because the list belongs to the host: `timezones` is what the host's own
# timedatectl accepts, `cloudflare_zones` the domains the stored Cloudflare
# token reaches.
PANEL_OPTION_SOURCES = ("timezones", "cloudflare_zones")
# The named sources the graphical installer suggests values from.
GUI_SUGGESTION_SOURCES = ("ssh_keys",)
# The languages a client-facing text comes in: the installer's page speaks
# both, an inventory `.env` reads `en`.
LANGS = ("en", "fr")

KNOB_FIELDS = frozenset({"key", "residence", "var", "env", "panel", "step",
                         "label", "help", "depends", "required",
                         "gui_suggestions_from", "gui_choice"})
# The two options of the launcher's choice in front of a field: `public` leaves
# it empty, `private` asks for it.
GUI_CHOICES = ("public", "private")
GUI_STEP_FIELDS = frozenset({"name", "title", "doc", "note"})

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


def _check_text(where: str, field: str, value, required: tuple[str, ...]) -> None:
    """A client-facing text: language to string, every `required` language
    present and non-empty, and no language a reader does not know."""
    _require(isinstance(value, dict),
             f"{where}: {field} is not a mapping of language to text")
    unknown = set(value) - set(LANGS)
    _require(not unknown, f"{where}: {field} has languages {sorted(unknown)}; "
                          f"the readers know {LANGS}")
    for lang in required:
        text = value.get(lang)
        _require(isinstance(text, str) and bool(text.strip()),
                 f"{where}: {field} has no {lang} text")


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
    # The value a stored-nothing field shows and stands for: the literal the
    # converge falls back to, which tests/unit/test_panel_defaults_match_the_converge.py
    # holds equal. A secret has no default to show.
    default = panel.get("default")
    if default is not None:
        _require(panel["kind"] != "secret",
                 f"{key}: a secret field shows no default")
        _require(isinstance(default, str) and bool(default),
                 f"{key}: panel default must be a non-empty string")
        if options is not None:
            _require(default in options,
                     f"{key}: the default {default!r} is not one of {options}")
    source = panel.get("options_from")
    if source is not None:
        _require(source in PANEL_OPTION_SOURCES,
                 f"{key}: options_from {source!r} is not one of {PANEL_OPTION_SOURCES}")
        _require(panel["kind"] == "text",
                 f"{key}: options_from fills a text field's list; a choice "
                 "declares its own options")


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
    # An example illustrates a value that has no sensible default, such as a
    # server's address: shown beside the field and in the template's comment,
    # never filled in, because a pre-filled example is an answer nobody gave.
    example = env.get("example")
    if example is not None:
        _require(isinstance(example, str) and bool(example),
                 f"{key}: env example must be a non-empty string")
        _require(not default,
                 f"{key}: a knob with a default needs no example; the default "
                 "already illustrates it")
    reseed = env.get("reseed")
    if reseed is not None:
        _require(reseed is True, f"{key}: env reseed is `true` or absent")


def load(source: Path = SOURCE) -> dict:
    """Parse and validate the registry: the one place its shape is enforced."""
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
        unknown = set(step) - GUI_STEP_FIELDS
        _require(not unknown, f"gui_steps {name}: {sorted(unknown)} are not step fields")
        for field in ("title", "doc"):
            _require(field in step, f"gui_steps {name}: no {field}")
            _check_text(f"gui_steps {name}", field, step[field], LANGS)
        # Absent means nothing to add below the section's fields.
        if "note" in step:
            _check_text(f"gui_steps {name}", "note", step["note"], LANGS)
        step_names.append(name)

    seen: set[str] = set()
    for entry in [*secrets, *config]:
        _require(isinstance(entry, dict), f"knobs.yml: entry {entry!r} is not a mapping")
        key = entry.get("key")
        _require(isinstance(key, str) and key, f"knobs.yml: entry with no key: {entry!r}")
        _require(key not in seen, f"{key}: declared twice")
        seen.add(key)
        unknown = set(entry) - KNOB_FIELDS
        _require(not unknown, f"{key}: {sorted(unknown)} are not registry fields; "
                              "a maintainer's note is a # comment")
        if "panel" in entry:
            _check_panel(key, entry["panel"])
        # One place to edit each value: the `.env` the installer writes, or the
        # panel. A knob in both is a value the client changes in one and finds
        # the other still holding the old one.
        _require(not ("env" in entry and entry.get("panel")),
                 f"{key}: has an env home and a panel field; a value lives in "
                 "one place")
        if "step" in entry:
            _require(entry["step"] in step_names,
                     f"{key}: step {entry['step']!r} is not a declared gui step, "
                     f"so the launcher would have nowhere to ask for it")
            _require("env" in entry,
                     f"{key}: the installer asks only for what its .env keeps, "
                     "and this knob has no env home")
            _require("label" in entry,
                     f"{key}: a field the launcher asks for needs a label to "
                     "name it on the page")
            _check_text(key, "label", entry["label"], LANGS)
            _require("help" in entry,
                     f"{key}: a field the launcher asks for needs help to explain it")
            _check_text(key, "help", entry["help"], LANGS)
        elif "env" in entry:
            _require("help" in entry,
                     f"{key}: a key the .env carries needs help above it")
            _check_text(key, "help", entry["help"], ("en",))
        else:
            _require("help" not in entry,
                     f"{key}: help is read by the launcher and the .env, and this "
                     "knob is in neither")
        for field in ("required", "gui_suggestions_from", "label", "gui_choice"):
            if field in entry:
                _require("step" in entry,
                         f"{key}: {field} is read by the launcher, and this knob "
                         "has no step")
        if "gui_choice" in entry:
            choice = entry["gui_choice"]
            _require(isinstance(choice, dict) and sorted(choice) == sorted(GUI_CHOICES),
                     f"{key}: gui_choice labels exactly {GUI_CHOICES}")
            for option in GUI_CHOICES:
                _check_text(f"{key} gui_choice", option, choice[option], LANGS)
            _require(not entry.get("required"),
                     f"{key}: a field behind a gui_choice is required only when "
                     "`private` is chosen, so it cannot be required outright")
        if "required" in entry:
            _require(isinstance(entry["required"], bool),
                     f"{key}: required is not a boolean")
        if "gui_suggestions_from" in entry:
            _require(entry["gui_suggestions_from"] in GUI_SUGGESTION_SOURCES,
                     f"{key}: gui_suggestions_from is not one of "
                     f"{GUI_SUGGESTION_SOURCES}")

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
            _require(not entry["env"].get("reseed") or residence == "store",
                     f"{key}: reseed writes the .env value into the store, and "
                     f"a {residence} value is not stored")
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

    # The same mistake on the installer's page: a section with no field.
    asked = {e["step"] for e in [*secrets, *config] if "step" in e}
    orphan = [n for n in step_names if n not in asked]
    _require(not orphan, f"gui_steps: {orphan} ask for nothing")

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


def _env_by_section(doc: dict) -> dict[str, list[dict]]:
    by_section: dict[str, list[dict]] = {}
    for entry in doc["config"]:
        env = entry.get("env")
        if env:
            by_section.setdefault(env["section"], []).append(entry)
    return by_section


def env_knobs(doc: dict) -> list[dict]:
    """The config knobs the `.env` carries, in the order it prints them. seed
    prompts in this order too, so a client answering the prompts and a client
    editing the file walk the same sequence."""
    by_section = _env_by_section(doc)
    return [entry for section in doc["env_sections"]
            for entry in by_section.get(section["name"], [])]


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
    """An inventory `.env`: the preamble, then one block per section, then one
    commented, defaulted key per knob that declares an env home."""
    lines = _comment(doc["env_header"])
    by_section = _env_by_section(doc)

    for section in doc["env_sections"]:
        lines.append("")
        lines.append(f"# --- {section['title']} ".ljust(78, "-"))
        if section.get("doc"):
            lines.append("#")
            lines.extend(_comment(section["doc"]))
        for entry in by_section.get(section["name"], []):
            lines.append("")
            lines.extend(_comment(entry["help"]["en"]))
            options = entry["env"].get("options")
            if options:
                lines.extend(_comment(f"One of: {', '.join(options)}."))
            example = entry["env"].get("example")
            if example:
                lines.extend(_comment(f"For example: {example}"))
            lines.append(f"{entry['key']}={entry['env']['default']}")
    return "\n".join(lines) + "\n"

