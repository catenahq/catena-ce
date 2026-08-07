"""Every custom filter a template invokes must actually be registered.

Ansible resolves filters at RENDER time, so a template calling a filter that
no longer exists is not a startup error -- it is a converge that gets most of
the way through and then dies on the one task that renders that template:

    Syntax error in template: No filter named 'portainer_container_regex'.

Which is how it was found: bench 050b, 384 tasks in, on gatus-sync.env.j2 --
a Jinja template that a code search for the retired filter name did not
surface, because the search index covers .py and .yml and not .j2. The rename
had already been applied to all twelve call sites in task files.

The check is name-resolution only. Registered names come from actually
importing each filter_plugins module and reading FilterModule().filters(),
so it cannot drift from what Ansible will see. Builtins are listed explicitly
below rather than guessed at: any name that is neither registered nor a
builtin is a filter that does not exist.

Run: uv run pytest tests/unit/test_filter_references_resolve.py
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

ANSIBLE = Path(__file__).resolve().parents[2]
PLUGIN_DIR = ANSIBLE / "playbooks" / "filter_plugins"

# Filter uses are only meaningful INSIDE a Jinja expression. Scanning raw
# text instead would read every shell pipe as a filter -- `| grep`, `| head`,
# `| awk` are all over the compose templates and the shell tasks, and a YAML
# block scalar (`shell: |`) is itself a bare pipe.
_JINJA_SPAN = re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.DOTALL)
# Quoted literals are stripped from an expression before filters are read
# out of it: an alternation inside a regex argument is full of pipes, and
# `is regex('...|502 Bad Gateway|...')` would otherwise report `received`,
# `context` and `cannot` as missing filters.
_STRING_LITERAL = re.compile(r"'[^']*'|\"[^\"]*\"")
# `| name(` or `| name }}` or `| name |`. Deliberately loose on the left --
# it is the name that matters, not what it is applied to.
_FILTER_USE = re.compile(r"\|\s*([a-z_][a-z0-9_]*)\s*[(}|\s]")

# Jinja2 + ansible-core builtins in use across the tree. Explicit, because
# the whole point is that an UNKNOWN name is a bug rather than a builtin
# nobody happens to have listed yet.
_BUILTINS = frozenset({
    # jinja2
    "abs", "attr", "batch", "capitalize", "center", "default", "dictsort",
    "escape", "first", "float", "forceescape", "format", "groupby", "indent",
    "int", "join", "last", "length", "list", "lower", "map", "max", "min",
    "pprint", "random", "reject", "rejectattr", "replace", "reverse", "round",
    "safe", "select", "selectattr", "slice", "sort", "string", "striptags",
    "sum", "title", "tojson", "trim", "truncate", "unique", "upper",
    "urlencode", "urlize", "wordcount", "wordwrap", "xmlattr", "items",
    # ansible-core
    "b64decode", "b64encode", "basename", "bool", "checksum", "combine",
    "comment", "comment_ify", "dict2items", "dirname", "difference",
    "expanduser", "product",
    "expandvars", "extract", "flatten", "from_json", "from_yaml",
    "from_yaml_all", "hash", "human_readable", "human_to_bytes",
    "intersect", "ipaddr", "mandatory", "md5", "password_hash", "path_join",
    "quote", "realpath", "regex_escape", "regex_findall", "regex_replace",
    "regex_search", "rekey_on_member", "relpath", "sha1", "shuffle",
    "splitext", "split", "strftime", "subelements", "symmetric_difference",
    "ternary", "to_datetime", "to_json", "to_nice_json", "to_nice_yaml",
    "to_uuid", "to_yaml", "type_debug", "union", "urlsplit", "vault",
    "win_basename", "win_dirname", "win_splitdrive", "zip", "zip_longest",
})


def _registered() -> set[str]:
    """Import each plugin and read FilterModule().filters() -- the same dict
    Ansible reads, so this cannot drift from the real registration."""
    names: set[str] = set()
    for path in sorted(PLUGIN_DIR.glob("*.py")):
        spec = importlib.util.spec_from_file_location(path.stem, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        names.update(mod.FilterModule().filters().keys())
    return names


# Vendored trees carry their own templates and are not ours to gate.
_SKIP = (".collections", ".venv", "__pycache__", "/tests/")


def _sources():
    for pattern in ("*.j2", "*.yml"):
        for path in ANSIBLE.rglob(pattern):
            text = str(path)
            if any(frag in text for frag in _SKIP):
                continue
            yield path


def test_every_filter_reference_resolves():
    known = _registered() | _BUILTINS
    unresolved: list[str] = []
    for path in _sources():
        body = path.read_text(encoding="utf-8", errors="replace")
        for span in _JINJA_SPAN.finditer(body):
            expr = _STRING_LITERAL.sub("''", span.group(0))
            for name in _FILTER_USE.findall(expr):
                if name in known:
                    continue
                lineno = body.count("\n", 0, span.start()) + 1
                unresolved.append(
                    f"{path.relative_to(ANSIBLE)}:{lineno}: {name}")
    assert not unresolved, (
        "template(s) invoke a filter that is neither registered by "
        "playbooks/filter_plugins nor an Ansible builtin. Ansible resolves "
        "filters at RENDER time, so this fails mid-converge on whichever "
        "task renders the template:\n  " + "\n  ".join(unresolved)
    )


def test_the_plugin_dir_actually_registered_something():
    """A guard on the guard: if importing the plugins silently produced an
    empty set, the check above would pass against builtins alone and prove
    nothing."""
    assert "swarm_container_regex" in _registered()
