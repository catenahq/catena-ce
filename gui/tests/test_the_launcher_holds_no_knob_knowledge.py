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
    so a hand-kept list here could not satisfy it."""
    for step in registry.steps(doc):
        built = next(s for s in steps_mod.build(doc) if s.name == step["name"])
        declared = [e["key"] for e in registry.step_fields(doc, step["name"])]
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


def test_the_probes_name_only_keys_the_registry_declares():
    """The other direction. A probe that checks a key nobody declares is a
    proof about a value no page collects."""
    declared = set()
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


def test_every_step_has_a_probe(doc):
    """A step with no probe advances on anything typed into it. The plan's
    rule is that every step validates before it advances, and a missing entry
    here is the quiet way to lose that."""
    for step in registry.steps(doc):
        assert step["name"] in steps_mod.PROBES, (
            f"{step['name']} has no check, so it would advance on any answer")


def test_a_field_hidden_by_its_governing_choice_is_not_asked(doc):
    """Offering a Headscale server address to a client who chose the hosted
    network asks for a value nothing will read; offering an OAuth pair to one
    who chose no private network asks for a credential to a network their
    server does not join."""
    access = next(s for s in steps_mod.build(doc) if s.name == "access")
    by_key = {f.key: f for f in access.fields}

    tailnet = {"ACCESS_METHOD": "tailnet", "TAILNET_PROVIDER": "tailscale"}
    assert by_key["tailscale_oauth_client_id"].shown_for(tailnet)
    assert not by_key["TAILNET_CONTROL_URL"].shown_for(tailnet)

    headscale = {"ACCESS_METHOD": "tailnet", "TAILNET_PROVIDER": "headscale"}
    assert by_key["TAILNET_CONTROL_URL"].shown_for(headscale)
    assert not by_key["tailscale_oauth_client_id"].shown_for(headscale)
