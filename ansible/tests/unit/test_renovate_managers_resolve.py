"""Every Renovate manager and scanctl image pin still finds the pin it tracks.

A regex manager fails silently in both directions that matter: a path that
does not resolve extracts nothing, and a resolving path whose pin has changed
shape extracts nothing either. Renovate treats neither as an error. The
dependency stops appearing, no PR is opened for it again, and the pin sits
frozen looking exactly like a pin nobody needs to move.

Two breaks reach it. A role directory that moves takes every manager anchored
on the old path with it, all at once. A pin that changes shape in place -- a
scalar folded into a Jinja expression, a variable renamed -- takes only its
own, which is harder to notice.

scanctl's image_pins are the list of every image pin this repo ships: scanctl
scans them, and the catena-admin update engine's repository job (ops
bump_versions.py) moves them. A pin that stops resolving is an image nothing
scans and nothing updates. scanctl fails its own run on an unresolvable pin;
this asserts the same thing where it is cheap to fix, and covers the Renovate
half, which has no such gate.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
RENOVATE = REPO / "renovate.json"
SCANCTL = REPO / "scanctl.yml"


def _managers() -> list[dict]:
    return json.loads(RENOVATE.read_text()).get("customManagers") or []


def _pin_file(manager: dict) -> Path:
    """The single file a manager's managerFilePatterns anchors on.

    Every manager here uses one fully-anchored literal path rather than a
    glob, which is what makes the file resolvable from the pattern.
    """
    patterns = manager["managerFilePatterns"]
    assert len(patterns) == 1, f"expected one pattern, got {patterns}"
    rel = patterns[0].strip("/").lstrip("^").rstrip("$").replace("\\.", ".")
    assert "*" not in rel, f"glob patterns are not resolvable here: {rel}"
    return REPO / rel


@pytest.mark.parametrize(
    "manager", _managers(), ids=lambda m: m["depNameTemplate"])
def test_the_manager_points_at_a_file_that_exists(manager):
    path = _pin_file(manager)
    assert path.is_file(), (
        f"{manager['depNameTemplate']}: {path.relative_to(REPO)} does not "
        "exist, so this manager extracts nothing and Renovate opens no PR "
        "for it -- indistinguishable from an up-to-date pin")


@pytest.mark.parametrize(
    "manager", _managers(), ids=lambda m: m["depNameTemplate"])
def test_the_manager_extracts_a_current_value(manager):
    """Python's re wants (?P<name>...); Renovate's Go RE2 wants (?<name>...).
    The syntaxes are otherwise the same for what these patterns use."""
    path = _pin_file(manager)
    if not path.is_file():
        pytest.skip("no such pin file; the sibling test reports it")
    text = path.read_text()
    found: list[str] = []
    for pattern in manager["matchStrings"]:
        found += re.findall(pattern.replace("(?<", "(?P<"), text)
    assert found, (
        f"{manager['depNameTemplate']}: no matchString matched "
        f"{_pin_file(manager).relative_to(REPO)}. The pin changed shape and "
        "this manager has stopped tracking it.")


def _image_pins() -> list[dict]:
    return yaml.safe_load(SCANCTL.read_text()).get("image_pins") or []


def _pin_id(pin: dict) -> str:
    return pin.get("repo") or Path(pin["file"]).parent.parent.name


@pytest.mark.parametrize("pin", _image_pins(), ids=_pin_id)
def test_the_scanctl_image_pin_resolves_a_ref(pin):
    """One capturing group, yielding either a whole <repo>:<tag> or -- when
    `repo` is set -- the tag that completes it. scanctl fails the run on a pin
    that matches nothing, so this is the same assertion made where the pin is
    cheap to fix rather than after a red CI run."""
    path = REPO / pin["file"]
    assert path.is_file(), (
        f"{_pin_id(pin)}: file {pin['file']} does not exist, so this image "
        "is never scanned")
    rx = re.compile(pin["pattern"])
    assert rx.groups == 1, (
        f"{_pin_id(pin)}: pattern {pin['pattern']!r} has {rx.groups} groups, "
        "scanctl needs exactly 1")
    found = rx.findall(path.read_text())
    assert found, (
        f"{_pin_id(pin)}: pattern {pin['pattern']!r} matched nothing in "
        f"{pin['file']}. The pin changed shape and this image has stopped "
        "being scanned.")
    for value in found:
        ref = f"{pin['repo']}:{value}" if pin.get("repo") else value
        assert ":" in ref and not ref.endswith(":"), (
            f"{_pin_id(pin)}: resolved {ref!r}, which is not a <repo>:<tag>")


def test_every_pinned_image_the_repo_ships_is_scanned():
    """A role default that pins an image nothing lists here is an image on
    every client host that no scan looks at. The absent ones are absent on
    purpose: the tier-1 control plane and cloudflared are pinned in
    catena-admin and scanned there, against the tags the host actually reads.
    """
    declared = {p["repo"] for p in _image_pins() if p.get("repo")}
    for repo in (
        "twinproduction/gatus",
        "healthchecks/healthchecks",
        "henrygd/beszel",
        "henrygd/beszel-agent",
        "quay.io/phasetwo/phasetwo-keycloak",
        "adorsys/keycloak-config-cli",
        "quay.io/oauth2-proxy/oauth2-proxy",
    ):
        assert repo in declared, f"{repo} is unscanned again: {sorted(declared)}"
    whole_ref = [p["pattern"] for p in _image_pins() if not p.get("repo")]
    assert any("clamav" in p for p in whole_ref), "clamav is unscanned again"
