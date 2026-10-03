"""Every question the launcher asks comes from the registry.

A question the launcher held its own copy of could ask for a value the
server does not accept, on the page where a client has no way of knowing. So
there is no list of fields in this package: adding a knob with a `step` puts it
on that page, and removing one takes it off.

Run: uv run pytest tests/test_the_launcher_holds_no_knob_knowledge.py
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from catena_gui import i18n, registry, steps as steps_mod

PACKAGE = Path(__file__).resolve().parents[1] / "catena_gui"


@pytest.fixture(scope="module")
def doc() -> dict:
    return registry.load()


def test_the_registry_is_found_and_current(doc):
    """A launcher that fell back to an empty registry would render blank pages
    and then produce an install.yaml that answered nothing."""
    assert doc["version"] == 1
    assert doc["gui_steps"], "the registry declares no installer pages"


def test_every_section_asks_for_something_and_names_it(doc):
    """An empty section is a heading nobody fills in, and a field is named on
    the page by the label the registry gives it."""
    for step in steps_mod.build(doc):
        assert step.fields, f"{step.name} asks for nothing"
        for field in step.fields:
            for lang in i18n.LANGS:
                assert field.label[lang].strip(), f"{field.key} has no {lang} label"


def test_a_page_shows_exactly_what_the_registry_gives_it(doc):
    """Read, not transcribed. The comparison is against the registry itself,
    so a hand-kept list here could not satisfy it. The one addition is the
    install contract's provider password, which the server section needs for a
    server that does not accept the key yet and which no knob declares,
    because nothing keeps it."""
    for step in registry.steps(doc):
        built = next(s for s in steps_mod.build(doc) if s.name == step["name"])
        declared = [e["key"] for e in registry.step_fields(doc, step["name"])]
        if step["name"] == "target":
            declared.append(steps_mod.PROVIDER_PASSWORD)
        assert [f.key for f in built.fields] == declared


def test_no_field_name_is_written_down_in_this_package():
    """The test with teeth. A literal knob key anywhere in the launcher is a
    copy of the registry, and a copy is what drifts.

    The probes are allowed to name keys -- proving a Cloudflare token works
    means naming it -- so they are read from steps.py's own declaration of
    which ones it probes rather than banned outright. What is banned is a key
    appearing in the code that RENDERS pages.
    """
    rendering = ("server.py", "render.py", "run.py", "registry.py", "i18n.py")
    declared = set()
    for entry in [*registry.load()["secrets"], *registry.load()["config"]]:
        declared.add(entry["key"])
    offenders = []
    for name in rendering:
        tree = ast.parse((PACKAGE / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value in declared:
                    offenders.append(f"{name}: {node.value}")
    assert not offenders, (
        "these files name registry keys directly, so they hold a copy of the "
        f"declaration they are supposed to read: {offenders}")


# Names in steps.py that look like knobs and are not: the environment variable
# helpers/install_key.py reads the provider's password from.
_NOT_KNOBS = {"INSTALL_KEY_PASSWORD"}


def test_the_probes_name_only_keys_the_registry_declares():
    """The other direction. A probe that checks a key nobody declares is a
    proof about a value no page collects."""
    declared = set(_NOT_KNOBS)
    doc = registry.load()
    for entry in [*doc["secrets"], *doc["config"]]:
        declared.add(entry["key"])
    tree = ast.parse((PACKAGE / "steps.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        value = node.value
        # Only SCREAMING or snake_case names that look like knobs, so prose in
        # a docstring is not mistaken for a key.
        if value.isupper() and "_" in value and value not in declared:
            raise AssertionError(
                f"steps.py probes {value}, which the registry does not declare")


def test_every_probe_checks_a_declared_section(doc):
    """A probe under a name no section has would never run."""
    declared = {step["name"] for step in registry.steps(doc)}
    assert set(steps_mod.PROBES) <= declared
    assert "target" in steps_mod.PROBES


def test_the_suggested_keys_are_the_pairs_on_this_machine(tmp_path):
    """A private key with its public half beside it, and nothing else: a
    lone private key or a stray .pub is not a pair the install can use."""
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    for name in ("id_ed25519", "id_ed25519.pub", "catena_ed25519",
                 "catena_ed25519.pub", "lonely", "stray.pub", "config"):
        (ssh / name).write_text("x")
    got = registry.suggestions_for({"gui_suggestions_from": "ssh_keys"}, ssh)
    assert [Path(p).name for p in got] == ["catena_ed25519", "id_ed25519"]
    assert registry.suggestions_for({}, ssh) == []
