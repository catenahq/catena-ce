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


def test_the_catena_admin_floor_is_a_version_not_a_moving_tag():
    """A floating tag cannot be bumped OR verified. The update engine
    classifies `latest` as floating and skips it outright, so the panel could
    never be bumped by the lane at all; and a tag that moves under a recorded
    digest turns the payload gate into a coin flip. It also froze the container
    at whatever digest swarm resolved on install day while roles/payload
    reinstalled the host engines from the same moving tag on every converge --
    new engines, old shell, widening every release."""
    floor = str(_image_defaults()["catena_admin_image_floor"][1])
    tag = floor.rsplit(":", 1)[-1]
    assert re.fullmatch(r"v?\d+\.\d+\.\d+", tag), (
        f"catena_admin_image_floor is {floor!r}; it must name a published "
        "version, not a moving tag")


def test_the_shipped_digest_applies_only_to_the_shipped_image():
    """An overridden image has a digest nobody wrote down -- the bench builds
    its own, and the update lane may have pinned a newer version. Asserting the
    floor's digest against one of those fails closed on a correct host."""
    payload = yaml.safe_load(
        (ROLES / "payload" / "defaults" / "main.yml").read_text())
    expr = str(payload["catena_payload_image_digest"])
    assert "catena_admin_image_floor_digest" in expr
    assert "catena_payload_image == catena_admin_image_floor" in expr
    common = yaml.safe_load(
        (ROLES / "common" / "defaults" / "main.yml").read_text())
    assert re.fullmatch(r"sha256:[0-9a-f]{64}",
                        str(common["catena_admin_image_floor_digest"]))


def test_the_engines_and_the_shell_come_from_one_image():
    """roles/payload runs at 5.5 and roles/catena-admin at 13, so neither can
    see the other's defaults. The value they share has to be declared in a role
    that runs before both, or the earlier one silently uses a fallback and the
    two agree only by coincidence of spelling."""
    common = yaml.safe_load(
        (ROLES / "common" / "defaults" / "main.yml").read_text())
    assert "catena_admin_image" in common
    assert "catena_admin_image_floor" in common
    payload = yaml.safe_load(
        (ROLES / "payload" / "defaults" / "main.yml").read_text())
    assert "catena_admin_image" in str(payload["catena_payload_image"])
    assert "ghcr.io" not in str(payload["catena_payload_image"]), (
        "roles/payload is carrying its own copy of the image reference again; "
        "that copy is what it actually used, because catena-admin's defaults "
        "are not in scope at role 5.5")


def test_the_converge_publishes_the_pins_before_any_role_reads_them():
    """The filter defaults to {} so a role cannot fail on a fresh host, which
    also means a loader that stopped publishing would be invisible: every
    image would silently resolve to its floor again."""
    loader = (Path(__file__).resolve().parents[2] / "playbooks" / "tasks"
              / "load_onbox_config.yml").read_text()
    assert "--emit image-pins" in loader
    assert "catena_image_pins" in loader
