"""Every image a role deploys must resolve through the on-host pin.

The defect this guards is invisible in review and silent in production: a role
templates an image straight from its own default, the converge asserts that
version, and every bump the on-host update lane applied between converges is
undone. The service comes back green on an older image and nothing reports it.

So the rule is structural rather than remembered -- an image default either
resolves through catena_image_pin, or it is on the list below with a reason.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ROLES = Path(__file__).resolve().parents[2] / "roles"

_IMAGE_VAR = re.compile(r"^[a-z][a-z0-9_]*_image$")

# Image variables that legitimately do NOT carry the filter. One reviewed entry
# at a time: an inferred rule ("anything ending in _floor is fine") is how the
# next one slips through.
RESOLVED_ELSEWHERE = {
    # The floor itself -- the input to the resolution, not a result of it.
    "catena_admin_image_floor":
        "the floor catena_admin_image resolves against",
    # Derives from catena_admin_image, which is already resolved. Resolving it
    # a second time here is how a host ends up extracting engines from one
    # image while running the shell from another.
    "catena_payload_image":
        "derives from catena_admin_image, resolved once in roles/catena-admin",
}


def _image_defaults() -> dict[str, tuple[str, str]]:
    """{variable: (role, value)} for every *_image default across the roles."""
    out: dict[str, tuple[str, str]] = {}
    for path in sorted(ROLES.glob("*/defaults/main.yml")):
        data = yaml.safe_load(path.read_text()) or {}
        for key, value in data.items():
            if _IMAGE_VAR.match(str(key)) or str(key).endswith("_image_floor"):
                out[str(key)] = (path.parents[1].name, str(value))
    return out


def test_every_role_image_resolves_through_the_pin():
    unresolved = []
    for var, (role, value) in _image_defaults().items():
        if var in RESOLVED_ELSEWHERE:
            continue
        if "catena_image_pin" not in value:
            unresolved.append(f"{role}: {var} = {value.strip()!r}")
    assert not unresolved, (
        "these images are asserted by the converge rather than asked for, so "
        "the next converge reverts whatever the on-host update lane bumped "
        "them to:\n  " + "\n  ".join(unresolved) + "\n"
        "Route the value through | catena_image_pin(catena_image_pins | "
        "default({})), or declare it in RESOLVED_ELSEWHERE with a reason."
    )


def test_the_scan_actually_finds_the_images():
    """A glob that stops matching turns the assertion above into a no-op."""
    found = _image_defaults()
    assert len(found) >= 8, f"only found {sorted(found)}"
    for expected in ("gatus_image", "keycloak_image", "oauth2_proxy_image",
                     "catena_admin_image"):
        assert expected in found, f"{expected} is no longer being scanned"


def test_the_exemptions_are_real_variables():
    """A stale exemption is a hole: the variable it names is gone, and a NEW
    variable of the same name would inherit the pass."""
    found = _image_defaults()
    for var in RESOLVED_ELSEWHERE:
        assert var in found, (
            f"{var} is declared exempt but no longer exists; drop the entry "
            "rather than leaving a name that a future variable could reuse")


def test_the_converge_publishes_the_pins_before_any_role_reads_them():
    """The filter defaults to {} so a role cannot fail on a fresh host, which
    also means a loader that stopped publishing would be invisible: every
    image would silently resolve to its floor again."""
    loader = (Path(__file__).resolve().parents[2] / "playbooks" / "tasks"
              / "load_onbox_config.yml").read_text()
    assert "--emit image-pins" in loader
    assert "catena_image_pins" in loader
