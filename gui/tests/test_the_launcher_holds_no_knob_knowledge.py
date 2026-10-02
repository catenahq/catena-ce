"""Every question the launcher asks comes from the registry.

THE PROPERTY THIS PINS. The knob registry exists because the same names lived
in four places and eight of them drifted across two sessions. The launcher is
the fifth reader, and it is the one a client meets first: a question it holds
its own copy of is a question that can ask for a value the server does not
accept, on the page where a client has no way of knowing.

So there is no list of fields in this package. Adding a knob with a `step`
puts it on that page; removing one takes it off. These tests are what makes
that true rather than a convention.

Run: uv run pytest tests/test_the_launcher_holds_no_knob_knowledge.py
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from catena_gui import registry, steps as steps_mod

PACKAGE = Path(__file__).resolve().parents[1] / "catena_gui"


@pytest.fixture(scope="module")
def doc() -> dict:
    return registry.load()


def test_the_registry_is_found_and_current(doc):
    """A launcher that fell back to an empty registry would render blank pages
    and then produce an install.yaml that answered nothing."""
    assert doc["version"] == 1
    assert doc["gui_steps"], "the registry declares no installer pages"


def test_every_page_asks_for_something_except_the_last(doc):
    """The last page is an acknowledgement rather than a form, which is why it
    is the one exception. Any other empty page is a heading nobody filled in."""
    built = steps_mod.build(doc)
    for step in built[:-1]:
        assert step.fields, f"{step.name} asks for nothing"
    assert built[-1].name == "keyset"
    assert not built[-1].fields


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
    rendering = ("server.py", "render.py", "run.py", "registry.py")
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


def test_every_step_that_says_what_it_proves_has_a_probe(doc):
    """A step whose `validates` line promises a proof and has no probe would
    advance on anything typed into it. A step with no `validates` has nothing
    to observe, and no probe either, rather than one that passes on anything."""
    for step in registry.steps(doc):
        if step.get("validates"):
            assert step["name"] in steps_mod.PROBES, (
                f"{step['name']} has no check, so it would advance on any answer")
        else:
            assert step["name"] not in steps_mod.PROBES, (
                f"{step['name']} declares nothing to prove and still has a probe")


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
