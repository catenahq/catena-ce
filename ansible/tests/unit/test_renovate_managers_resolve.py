"""Every Renovate custom manager and Trivy matrix entry still finds its pin.

A regex manager fails silently in both directions that matter. Point it at a
path that no longer exists and it extracts nothing; leave the path right but
let the pin change shape underneath it and it extracts nothing either. Renovate
reports neither as an error -- the dependency simply stops appearing, no PR is
ever opened again, and the pin sits frozen looking exactly like a pin nobody
has needed to move.

Both happened here. `roles/` became `bootstrap/roles/` + `reconcile/roles/` and
all seven managers went dark at once. Before that, clamav's pin moved inside a
folded Jinja expression and its manager -- still pointing at a file that
existed -- had been extracting nothing on its own.

The Trivy image matrix reads the same pins from the same files, so a manager
that cannot find its pin is also an image that stops being scanned.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
RENOVATE = REPO / "renovate.json"
TRIVY = REPO / ".github" / "workflows" / "trivy.yml"


def _managers() -> list[dict]:
    return json.loads(RENOVATE.read_text())["customManagers"]


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


def _trivy_matrix() -> list[dict]:
    """The pins job's matrix, read out of the heredoc it is declared in."""
    body = TRIVY.read_text()
    block = body.split("cat > matrix.json <<'EOF'")[1].split("EOF")[0]
    return json.loads(block.strip())


@pytest.mark.parametrize("entry", _trivy_matrix(), ids=lambda e: e["name"])
def test_the_trivy_matrix_entry_resolves_an_image(entry):
    """The matrix selects entries whose pin_file the PR touched, so a stale
    pin_file is never selected, the pins job skips, and trivy-gate goes green
    over a run that scanned nothing. On push/cron the same stale path instead
    fails the resolve step. Neither reads as "this image has no CVEs"."""
    path = REPO / entry["pin_file"]
    assert path.is_file(), (
        f"{entry['name']}: pin_file {entry['pin_file']} does not exist, so "
        "this image is never scanned on a PR that touches it")
    # Mirrors the workflow's resolve step: narrow to the pinning line, then
    # take the last quoted value on it (either quote style).
    line = next((m for m in re.findall(entry["pin_pattern"], path.read_text())), "")
    assert line, (
        f"{entry['name']}: pin_pattern {entry['pin_pattern']!r} matched "
        f"nothing in {entry['pin_file']}")
    quoted = re.findall(r"[\"'][^\"']+[\"']", line)
    value = quoted[-1].strip("\"'") if quoted else line
    assert value, f"{entry['name']}: resolved an empty tag"
