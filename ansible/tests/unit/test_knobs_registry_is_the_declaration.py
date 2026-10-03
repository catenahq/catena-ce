"""helpers/knobs.yml is the registry: it validates, and what onbox_config
derives from it holds.

The derived part: which credentials the store accepts, which knobs reach the
converge as facts, and that the three residences partition rather than overlap.
Plus the two failure modes of reading a file instead of holding a literal -- an
absent registry has to raise rather than empty, and an explicit path has to win.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

ANSIBLE_DIR = Path(__file__).resolve().parents[2]
if str(ANSIBLE_DIR) not in sys.path:
    sys.path.insert(0, str(ANSIBLE_DIR))

from helpers import onbox_config, render_knobs  # noqa: E402


@pytest.fixture(scope="module")
def registry() -> dict:
    """Every shape rule in render_knobs.load() holds on the shipped file."""
    return render_knobs.load()


def test_onbox_config_reads_the_same_registry(registry):
    assert onbox_config._KNOBS == registry


def test_the_store_accepts_every_declared_credential(registry):
    """EXTERNAL_SECRETS is the allowlist apply_inputs enforces, and it is the
    registry's secrets. A credential in one and not the other is either a key
    the client can be told to enter and the store refuses, or a key the store
    accepts that nothing asks for."""
    assert {s["key"] for s in registry["secrets"]} == set(onbox_config.EXTERNAL_SECRETS)


def test_only_projected_store_knobs_reach_the_converge(registry):
    """A stored knob with no declared `var` stays out of SETTINGS_CONFIG.

    Those are read straight off the store by a runtime lane with no converge in
    between, so there is no fact to publish. Publishing one anyway would create
    an Ansible variable no role reads.
    """
    unprojected = {
        k["key"] for k in registry["config"]
        if k["residence"] == "store" and "var" not in k
    }
    assert unprojected, "the fixture is meaningless if every store knob projects"
    assert not (unprojected & set(onbox_config.SETTINGS_CONFIG))


def test_residences_do_not_overlap(registry):
    """The three residences partition the config knobs.

    A key in two of them would be read from two places, and the `.env` copy
    would quietly win on a tag-scoped converge that skipped the store loader --
    which is the failure the two-owner split was drawn to prevent.
    """
    store = set(onbox_config.SETTINGS_CONFIG)
    controller = set(onbox_config.BOOTSTRAP_CONFIG)
    host = {k["key"] for k in registry["config"] if k["residence"] == "host"}
    assert not (store & controller)
    assert not (store & host)
    assert not (controller & host)


def test_a_missing_registry_raises_rather_than_emptying(monkeypatch, tmp_path):
    """An install that lost the file is broken, and this is the only place that
    can say so.

    Falling back to empty sets would leave apply_inputs refusing every
    credential the client supplies and the converge publishing no config facts,
    both silent and both indistinguishable from a host nobody configured yet.
    """
    monkeypatch.setenv("CATENA_KNOBS", str(tmp_path / "absent.json"))
    monkeypatch.setenv("CATENA_PAYLOAD_LIB", str(tmp_path))
    monkeypatch.setattr(onbox_config, "__file__", str(tmp_path / "onbox_config.py"))
    with pytest.raises(FileNotFoundError):
        onbox_config._knobs_path()


def test_an_explicit_path_wins(tmp_path, monkeypatch):
    """CATENA_KNOBS is how a test or an odd host layout points at a registry."""
    target = tmp_path / "elsewhere.json"
    target.write_text("{}")
    monkeypatch.setenv("CATENA_KNOBS", str(target))
    assert onbox_config._knobs_path() == target


_STAGED_KNOBS = "/root/.catena-knobs.json"


def _script_tasks_running_the_store(node):
    if isinstance(node, dict):
        script = node.get("ansible.builtin.script")
        if isinstance(script, dict) and "onbox_config.py" in str(script.get("cmd", "")):
            yield node
        for value in node.values():
            yield from _script_tasks_running_the_store(value)
    elif isinstance(node, list):
        for item in node:
            yield from _script_tasks_running_the_store(item)


def test_every_remote_store_call_names_the_staged_registry():
    """The script module copies onbox_config.py to the host ALONE, so the
    registry beside it in the checkout never arrives. On a host the payload has
    not reached yet -- every bootstrap, every first converge -- there is no
    installed copy either, and the module raises at import. Each call has to
    name the copy the loader staged."""
    calls = []
    for path in ANSIBLE_DIR.rglob("*.yml"):
        if ".collections" in path.parts:
            continue
        doc = yaml.safe_load(path.read_text())
        for task in _script_tasks_running_the_store(doc):
            calls.append(path)
            env = task.get("environment") or {}
            assert env.get("CATENA_KNOBS") == _STAGED_KNOBS, (
                f"{path.relative_to(ANSIBLE_DIR)}: {task.get('name')!r} runs "
                f"onbox_config.py without CATENA_KNOBS={_STAGED_KNOBS}"
            )
    assert calls, "found no remote onbox_config.py call; the scan is broken"


def test_every_knob_has_exactly_one_owner(registry):
    """No key belongs to two residences, and none belongs to none: `host` is
    the target identity seed writes into hosts.yml."""
    for knob in registry["config"]:
        assert knob["residence"] in render_knobs.RESIDENCES, knob["key"]


def test_the_env_carries_every_key_that_declares_one(registry):
    """A rendered `.env` is the registry's env knobs and nothing else: a
    renderer that dropped a section would leave a key nobody can set."""
    declared = {k["key"]: k["env"]["default"] for k in registry["config"] if "env" in k}
    assert declared, "no knob declares an env home, so the .env is empty"

    in_template: dict[str, str] = {}
    for raw in render_knobs.render_env(registry).splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        in_template[key.strip()] = value.strip()

    assert in_template == declared


def test_a_section_with_no_key_is_refused(tmp_path):
    """A heading renders as a section break followed by the next section, so an
    empty one reads as a key having gone missing rather than as a heading nobody
    filled in."""
    doc = render_knobs.load()
    doc["env_sections"].append({"name": "orphan", "title": "Orphan"})
    source = tmp_path / "knobs.yml"
    source.write_text(yaml.safe_dump(doc, sort_keys=False))
    with pytest.raises(render_knobs.KnobError, match="orphan"):
        render_knobs.load(source)


def _refused(tmp_path, doc: dict, match: str) -> None:
    source = tmp_path / "knobs.yml"
    source.write_text(yaml.safe_dump(doc, sort_keys=False))
    with pytest.raises(render_knobs.KnobError, match=match):
        render_knobs.load(source)


def _entry(doc: dict, key: str) -> dict:
    return next(e for e in [*doc["secrets"], *doc["config"]] if e["key"] == key)


def test_launcher_fields_need_a_step_and_a_sound_shape(tmp_path):
    """`required` and the suggestion source are read by the launcher alone, so
    on a knob it never asks for they are read by nothing, and a malformed one
    would reach the page as a broken control."""
    doc = render_knobs.load()
    _entry(doc, "OPS_USER")["required"] = True
    _refused(tmp_path, doc, "has no step")

    doc = render_knobs.load()
    _entry(doc, "HOST_PUBLIC_IP")["required"] = "yes"
    _refused(tmp_path, doc, "not a boolean")

    doc = render_knobs.load()
    _entry(doc, "SSH_PRIVATE_KEY")["gui_suggestions_from"] = "planets"
    _refused(tmp_path, doc, "gui_suggestions_from")

    doc = render_knobs.load()
    _entry(doc, "OPS_USER")["label"] = "Ops user"
    _refused(tmp_path, doc, "has no step")

    doc = render_knobs.load()
    del _entry(doc, "HOST_PUBLIC_IP")["label"]
    _refused(tmp_path, doc, "needs a label")


def test_every_installer_section_asks_for_something(tmp_path):
    """A section with no field is a heading nobody fills in."""
    doc = render_knobs.load()
    doc["gui_steps"].append({"name": "empty", "title": {"en": "Empty", "fr": "Vide"},
                             "doc": {"en": "Nothing.", "fr": "Rien."}})
    _refused(tmp_path, doc, "ask for nothing")


def test_a_maintainer_note_is_a_comment_not_a_field(tmp_path):
    """A field nothing reads is prose that reaches a reader it was not written
    for, or no one: the registry refuses any field it does not know."""
    doc = render_knobs.load()
    _entry(doc, "COMMON_TIMEZONE")["doc"] = "why it is declared this way"
    _refused(tmp_path, doc, "not registry fields")

    doc = render_knobs.load()
    doc["gui_steps"][0]["validates"] = "what it proves"
    _refused(tmp_path, doc, "not step fields")


def test_client_copy_is_where_a_client_reads_it(tmp_path):
    """`help` on every installer field in both languages and on every `.env`
    key in English, and nowhere else."""
    doc = render_knobs.load()
    del _entry(doc, "HOST_SSH_PORT")["help"]["fr"]
    _refused(tmp_path, doc, "no fr text")

    doc = render_knobs.load()
    del _entry(doc, "OPS_USER")["help"]
    _refused(tmp_path, doc, "needs help")

    doc = render_knobs.load()
    _entry(doc, "COMMON_TIMEZONE")["help"] = {"en": "The time zone."}
    _refused(tmp_path, doc, "in neither")

    doc = render_knobs.load()
    _entry(doc, "HOST_PUBLIC_IP")["label"] = {"en": "IP", "fr": "IP", "de": "IP"}
    _refused(tmp_path, doc, "languages")


def test_every_installer_field_speaks_both_languages(registry):
    for entry in [*registry["secrets"], *registry["config"]]:
        if "step" in entry:
            for lang in render_knobs.LANGS:
                assert entry["label"][lang].strip(), (entry["key"], lang)
                assert entry["help"][lang].strip(), (entry["key"], lang)


def test_a_value_has_one_place_to_be_edited(tmp_path):
    """The `.env` or the panel, never both: a client who changes it in one
    finds the other still holding the old value."""
    doc = render_knobs.load()
    _entry(doc, "CLOUDFLARE_ZONE")["env"] = {"section": "admin", "default": ""}
    _refused(tmp_path, doc, "one place")


def test_the_installer_asks_only_for_what_its_env_keeps(tmp_path):
    """A step on a knob with no `.env` home is a question whose answer the
    installer has nowhere to write."""
    doc = render_knobs.load()
    _entry(doc, "CLOUDFLARE_ZONE")["step"] = "target"
    _refused(tmp_path, doc, "no env home")


def test_nothing_the_panel_edits_is_asked_at_install(registry):
    for entry in [*registry["secrets"], *registry["config"]]:
        assert not (entry.get("panel") and entry.get("step")), entry["key"]


def test_the_installers_required_fields_are_the_ones_it_cannot_install_without(
        registry):
    """The server's address, its initial login and SSH port, the keypair that
    reaches it, and the administrator's email. Nothing else blocks an install:
    the domain, the tailnet and the backup can all be entered in the panel."""
    required = {e["key"] for e in [*registry["secrets"], *registry["config"]]
                if e.get("required")}
    assert required == {"HOST_PUBLIC_IP", "HOST_INITIAL_USER", "HOST_SSH_PORT",
                        "SSH_PRIVATE_KEY", "ADMIN_EMAIL"}


def test_a_default_that_needs_quoting_is_refused():
    """A template default is an illustration, and one that renders a line
    parsing back as something else is the wrong illustration."""
    with pytest.raises(render_knobs.KnobError, match="unquoted"):
        render_knobs._check_env(
            "SOME_KEY", {"section": "host", "default": "two words"}, {"host"})
