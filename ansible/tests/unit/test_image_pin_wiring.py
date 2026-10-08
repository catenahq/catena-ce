"""Every image a role deploys must resolve through the on-host pin.

The defect this guards is invisible in review and silent in production: a role
templates an image straight from its own default, the converge asserts that
version, and every bump the on-host update lane applied between converges is
undone. The service comes back green on an older image and nothing reports it.

So the rule is structural rather than remembered -- an image default either
resolves through catena_image_pin, or it is on the list below with a reason.

The catena-admin image is the one on that list: a converge runs the image it
was started with, which install-host.sh and catena-converge name in
CATENA_ADMIN_IMAGE, and the config loader stops a converge that names none.
"""
from __future__ import annotations

import re
from pathlib import Path

import jinja2
import yaml

from ansible_tree import ROLE_ROOTS as _ROLE_ROOTS, role_dir as _role_dir

ANSIBLE = Path(__file__).resolve().parents[2]
# The values both sides share: a host converging itself runs no bootstrap role,
# so a constant the reconcile roles need cannot live in one.
SHARED_VARS = ANSIBLE / "playbooks" / "group_vars" / "all" / "main.yml"
LOADER = ANSIBLE / "playbooks" / "tasks" / "load_onbox_config.yml"


_IMAGE_VAR = re.compile(r"^[a-z][a-z0-9_]*_image$")

# Image variables that legitimately do NOT carry the filter. One reviewed entry
# at a time: an inferred rule ("anything ending in _floor is fine") is how the
# next one slips through.
RESOLVED_ELSEWHERE = {
    # The image the tree running the converge came from, named by whoever
    # started it. A pin applied on top would run engines and a panel from
    # another release than the tree.
    "catena_admin_image":
        "CATENA_ADMIN_IMAGE verbatim, set by install-host.sh and catena-converge",
    # The bootstrap fallback only. On a host that has a catena-admin service,
    # reconcile/roles/payload follows the SERVICE's image instead, so the engines and the
    # shell cannot resolve to different versions. Resolving the pin a second
    # time here is what made them able to.
    "catena_payload_image":
        "bootstrap fallback; steady state follows the catena-admin service",
}


def _image_defaults() -> dict[str, tuple[str, str]]:
    """{variable: (role, value)} for every *_image default across the roles
    and the shared group_vars."""
    out: dict[str, tuple[str, str]] = {}
    for path in sorted(p for root in _ROLE_ROOTS
                       for p in root.glob("*/defaults/main.yml")) + [SHARED_VARS]:
        data = yaml.safe_load(path.read_text()) or {}
        for key, value in data.items():
            # A *_image_floor is scanned too: a floor has to earn an
            # exemption rather than inherit one by not matching the glob.
            if _IMAGE_VAR.match(str(key)) or str(key).endswith(
                    ("_image_floor", "_image_override")):
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


def _render_image(env_value: str) -> str:
    shared = yaml.safe_load(SHARED_VARS.read_text())
    lookups = []

    def lookup(kind, name, **_):
        lookups.append((kind, name))
        return env_value

    out = jinja2.Environment().from_string(
        str(shared["catena_admin_image"])).render(lookup=lookup)
    assert lookups == [("env", "CATENA_ADMIN_IMAGE")], lookups
    return out


def test_the_panel_image_is_the_one_the_converge_was_started_with():
    """Verbatim, digest and all: the digest is what reconcile/roles/payload
    checks the engines against, and the release install-host.sh fetched or the
    image the panel runs is exactly what the tree came from."""
    for ref in ("ghcr.io/catenahq/catena-admin:v0.6.2@sha256:" + "b" * 64,
                "10.0.0.1:5001/catena-admin:bench-3@sha256:" + "c" * 64,
                "ghcr.io/catenahq/catena-admin:v0.6.2"):
        assert _render_image(ref) == ref
    assert _render_image("") == ""


def test_no_role_carries_a_hand_maintained_catena_admin_version():
    """A version literal in a default is a second answer to "which panel",
    and the converge would install it under a tree from another release."""
    for var, (role, value) in _image_defaults().items():
        if "catena-admin" not in str(value):
            continue
        assert not re.search(r"catena-admin:v?\d+\.\d+\.\d+", str(value)), (
            f"{role}: {var} names a catena-admin version literal ({value!r}). "
            "The converge runs the image CATENA_ADMIN_IMAGE names.")
    shared = yaml.safe_load(SHARED_VARS.read_text())
    for gone in ("catena_admin_image_floor", "catena_admin_image_floor_digest",
                 "catena_admin_image_override", "catena_admin_repository"):
        assert gone not in shared and gone not in _image_defaults(), (
            f"{gone} is back: a second input to the panel image beside "
            "CATENA_ADMIN_IMAGE")


def _loader_tasks() -> list[dict]:
    return yaml.safe_load(LOADER.read_text())


def _image_assert() -> dict:
    found = [t for t in _loader_tasks()
             if "ansible.builtin.assert" in t
             and any("catena_admin_image" in str(c)
                     for c in t["ansible.builtin.assert"]["that"])]
    assert len(found) == 1, (
        f"{LOADER.name} has {len(found)} asserts on catena_admin_image; the "
        "converge must refuse to run with no image, once, before any role")
    return found[0]["ansible.builtin.assert"]


def test_a_converge_that_names_no_image_stops_in_the_loader():
    """Every role after the loader would otherwise pull, extract or deploy an
    empty reference. The message names the two entry points that set it."""
    check = _image_assert()
    env = jinja2.Environment()
    for cond in check["that"]:
        holds = env.compile_expression(str(cond))
        assert not holds(catena_admin_image=""), cond
        assert holds(catena_admin_image="ghcr.io/catenahq/catena-admin:v0.6.2"), cond
    message = str(check["fail_msg"])
    for name in ("CATENA_ADMIN_IMAGE", "install-host.sh", "catena-converge"):
        assert name in message, f"the refusal does not name {name}"


def test_the_loader_looks_nothing_up_about_the_panel_image():
    """The image is named by whoever starts the converge. A registry lookup or
    a read of the running service here would be a second answer, reachable
    only by a converge nobody in the product starts."""
    text = LOADER.read_text()
    for gone in ("catena_admin_release", "catena_admin_fallback_image",
                 "docker", "service inspect"):
        assert gone not in text, f"{LOADER.name} reads {gone!r} again"


def test_the_engines_and_the_shell_come_from_one_image():
    """reconcile/roles/payload runs long before reconcile/roles/catena-admin, so
    neither can see the other's defaults. The value they share is declared in
    the shared group_vars, which every playbook loads -- including
    reconcile.yml on a host converging itself, which runs no bootstrap role --
    or the earlier one silently uses a fallback and the two agree only by
    coincidence of spelling."""
    shared = yaml.safe_load(SHARED_VARS.read_text())
    assert "catena_admin_image" in shared
    assert "catena_admin_service_name" in shared, (
        "reconcile/roles/payload reads it to find the service whose image the "
        "engines follow; reconcile/roles/catena-admin's defaults are not in "
        "scope there")
    admin = yaml.safe_load(
        (_role_dir("catena-admin") / "defaults" / "main.yml").read_text())
    assert "catena_admin_service_name" not in admin, (
        "declared in two roles is how the halves end up agreeing only by "
        "coincidence of spelling -- the defect the image already had")
    payload = yaml.safe_load(
        (_role_dir("payload") / "defaults" / "main.yml").read_text())
    assert "catena_admin_image" in str(payload["catena_payload_image"])
    assert "ghcr.io" not in str(payload["catena_payload_image"]), (
        "reconcile/roles/payload is carrying its own copy of the image reference again; "
        "that copy is what it actually used, because catena-admin's defaults "
        "are not in scope there")


def test_the_converge_publishes_the_pins_before_any_role_reads_them():
    """The filter defaults to {} so a role cannot fail on a fresh host, which
    also means a loader that stopped publishing would be invisible: every
    image would silently resolve to its floor again."""
    loader = LOADER.read_text()
    assert "--emit image-pins" in loader
    assert "catena_image_pins" in loader


def test_every_converge_checks_the_image_under_any_tag():
    """The loader is included with apply: tags [always], which is what keeps a
    tag-scoped converge from running with no image at all."""
    playbooks = ANSIBLE / "playbooks"
    for name in ("converge.yml", "reconcile.yml", "lockdown.yml",
                 "rotate-tailscale.yml"):
        includes = [
            t for play in yaml.safe_load((playbooks / name).read_text())
            for t in play.get("pre_tasks", [])
            if str(t.get("ansible.builtin.include_tasks", {}).get("file", ""))
            .endswith("load_onbox_config.yml")
        ]
        assert includes, f"{name} does not load the on-box config"
        for include in includes:
            applied = include["ansible.builtin.include_tasks"].get("apply", {})
            assert "always" in applied.get("tags", []), (
                f"{name} includes the loader without applying [always], so a "
                "--tags run skips the image check")
