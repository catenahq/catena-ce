"""A systemd lane's .service and .timer come from the SAME place.

A lane whose .service ships in the panel image while its .timer is rendered by
the converge has two owners and two release cadences: a change to the pair stays
half-applied until both a new image and a converge have landed, in that order,
and nothing on the host says which half it is on.

Nothing hard-fails, which is what makes it worth a gate: a stale timer still
fires, a new service still runs, and the mismatch surfaces as a lane doing
almost the right thing.

THE TEST FOR WHICH SIDE OWNS A UNIT is the same one the whole migration uses: if
its content is a function of the product VERSION it belongs in the image; if it
is a function of THIS HOST it belongs to the converge. A unit whose only
variables are compiled-in constants is version-shaped, and rendering it from a
template is a converge doing work an image already did.

Run: uv run pytest tests/unit/test_no_lane_is_half_owned.py
"""
from __future__ import annotations

import re
from pathlib import Path

_ANSIBLE = Path(__file__).resolve().parents[2]
_ADMIN = _ANSIBLE.parents[1] / "catena-admin" / "payload"

# Units the converge writes, whether the destination is literal or templated.
_DEST = re.compile(r"dest:\s*/etc/systemd/system/([A-Za-z0-9@._{}\s-]+\.(?:service|timer))")


def _converge_units() -> dict[str, str]:
    """unit stem -> the file that renders it, for both halves."""
    out: dict[str, str] = {}
    for side in ("bootstrap", "reconcile"):
        root = _ANSIBLE / side / "roles"
        if not root.is_dir():
            continue
        for path in root.rglob("*.yml"):
            text = path.read_text(encoding="utf-8", errors="ignore")
            for raw in _DEST.findall(text):
                # `{{ dashboard_sync_unit }}.timer` -> resolve the Jinja to the
                # role default that holds it, or keep it as written.
                name = raw.strip()
                for var in re.findall(r"\{\{\s*([a-z_][a-z0-9_]*)\s*\}\}", name):
                    m = re.search(rf'^{var}:\s*"?([A-Za-z0-9@._-]+)"?\s*$',
                                  (path.parents[1] / "defaults" / "main.yml").read_text(
                                      encoding="utf-8", errors="ignore")
                                  if (path.parents[1] / "defaults" / "main.yml").is_file()
                                  else "", re.M)
                    if m:
                        name = re.sub(rf"\{{\{{\s*{var}\s*\}}\}}", m.group(1), name)
                if "{" in name:
                    continue
                out[name] = str(path.relative_to(_ANSIBLE))
    return out


def _image_units() -> set[str]:
    if not _ADMIN.is_dir():
        return set()
    return {p.name for p in _ADMIN.rglob("systemd/*") if p.is_file()}


def _stem(unit: str) -> str:
    return unit.rsplit(".", 1)[0]


def test_the_scan_sees_both_sides():
    """Guards the guard. Either side coming back empty makes the comparison
    below pass over nothing, which is what a path convention change does to a
    scanner like this."""
    converge = _converge_units()
    image = _image_units()
    assert len(converge) >= 5, f"only {len(converge)} converge-rendered units: {converge}"
    if _ADMIN.is_dir():
        assert len(image) >= 10, f"only {len(image)} image-shipped units: {sorted(image)}"


def test_no_lane_has_one_half_in_each_place():
    image = _image_units()
    if not image:
        return  # sibling catena-admin absent; nothing to compare against
    converge = _converge_units()
    split: dict[str, str] = {}
    for unit, where in converge.items():
        for other in image:
            if _stem(other) == _stem(unit) and other != unit:
                split[_stem(unit)] = (
                    f"{unit} rendered by {where}, {other} shipped in the image")
    assert not split, (
        "these lanes have their .service and .timer owned by different sides, "
        "so a change to the pair is half-applied until both an image and a "
        f"converge have landed: {split}"
    )
