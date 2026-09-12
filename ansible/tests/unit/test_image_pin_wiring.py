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
import sys
from pathlib import Path

import jinja2
import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
_ROLE_ROOTS = (ANSIBLE / "bootstrap" / "roles",
               ANSIBLE / "reconcile" / "roles")

def _role_dir(name: str) -> Path:
    """Where a role lives, whichever side it is on.

    Phase 1b split roles/ into bootstrap/roles/ and reconcile/roles/. Resolved
    by search rather than by a hard-coded side so a role moving across the line
    -- which is a thing this migration does -- does not need this file edited
    too.
    """
    for root in _ROLE_ROOTS:
        if (root / name).is_dir():
            return root / name
    raise AssertionError(f"no role named {name} under {[str(r) for r in _ROLE_ROOTS]}")


_IMAGE_VAR = re.compile(r"^[a-z][a-z0-9_]*_image$")

# Image variables that legitimately do NOT carry the filter. One reviewed entry
# at a time: an inferred rule ("anything ending in _floor is fine") is how the
# next one slips through.
RESOLVED_ELSEWHERE = {
    # An explicit operator pin, and the input to the resolution rather than a
    # result of it. Empty unless CATENA_ADMIN_IMAGE is set.
    "catena_admin_image_override":
        "the operator override catena_admin_image resolves against",
    # The bootstrap fallback only. On a host that has a catena-admin service,
    # reconcile/roles/payload follows the SERVICE's image instead, so the engines and the
    # shell cannot resolve to different versions. Resolving the pin a second
    # time here is what made them able to.
    "catena_payload_image":
        "bootstrap fallback; steady state follows the catena-admin service",
}


def _image_defaults() -> dict[str, tuple[str, str]]:
    """{variable: (role, value)} for every *_image default across the roles."""
    out: dict[str, tuple[str, str]] = {}
    for path in sorted(p for root in _ROLE_ROOTS
                       for p in root.glob("*/defaults/main.yml")):
        data = yaml.safe_load(path.read_text()) or {}
        for key, value in data.items():
            # _image_floor is still scanned even though none is left: a
            # reintroduced floor has to earn an exemption rather than inherit
            # one by not matching the glob.
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


def test_no_role_carries_a_hand_maintained_catena_admin_version():
    """The version and its digest were two literals in bootstrap/roles/common, bumped by
    hand together on every release. They drifted -- v0.5.1 published as
    sha256:205a5a70... while the recorded digest stayed sha256:09e03d74... --
    and reconcile/roles/payload correctly refused to extract, so a correct host holding a
    correctly published image could not complete a fresh install.

    Both halves now come from one registry answer. A literal reappearing in a
    role default is that defect being rebuilt."""
    for var, (role, value) in _image_defaults().items():
        if "catena-admin" not in str(value):
            continue
        assert not re.search(r"catena-admin:v?\d+\.\d+\.\d+", str(value)), (
            f"{role}: {var} names a catena-admin version literal ({value!r}). "
            "The version is resolved from the registry by "
            "playbooks/tasks/load_onbox_config.yml, together with its digest, "
            "so the two cannot drift apart again.")
    common = yaml.safe_load(
        (_role_dir("common") / "defaults" / "main.yml").read_text())
    for gone in ("catena_admin_image_floor", "catena_admin_image_floor_digest"):
        assert gone not in common, (
            f"{gone} is back; it is half of a two-writer answer and the other "
            "half is the registry")
    assert "catena_admin_release" in str(common["catena_admin_image"]), (
        "catena_admin_image no longer reads the resolved release; a converge "
        "would install whatever the remaining literal says")


def _render_digest(payload_image, release, env_digest=""):
    """Render the catena_payload_image_digest expression under a fake context.

    Substring assertions cannot tell a correct comparison from one that never
    matches, and "never matches" is the dangerous direction: it reads as an
    unchecked extract, which looks exactly like a passing verification.
    """
    payload = yaml.safe_load(
        (_role_dir("payload") / "defaults" / "main.yml").read_text())
    env = jinja2.Environment()
    env.filters["regex_replace"] = (
        lambda s, find, repl: re.sub(find, repl, str(s)))
    return env.from_string(
        str(payload["catena_payload_image_digest"])
    ).render(
        lookup=lambda kind, name, default="": env_digest,
        catena_payload_image=payload_image,
        catena_admin_release=release,
    ).strip()


def _release(tag_ref, digest):
    return {"repository": tag_ref.rsplit(":", 1)[0], "version": tag_ref.rsplit(":", 1)[-1],
            "tag_ref": tag_ref, "digest": digest, "ref": f"{tag_ref}@{digest}"}


def test_the_resolved_digest_applies_only_to_the_resolved_image():
    """An overridden image has a digest this converge never resolved -- the
    bench builds its own, and the update lane may have pinned a version this
    converge did not ask for. Asserting the resolved digest against one of
    those fails closed on a correct host."""
    payload = yaml.safe_load(
        (_role_dir("payload") / "defaults" / "main.yml").read_text())
    expr = str(payload["catena_payload_image_digest"])
    assert "catena_admin_release" in expr

    rel = _release("ghcr.io/catenahq/catena-admin:v0.3.1", "sha256:" + "ab" * 32)
    assert _render_digest("ghcr.io/catenahq/catena-admin:v0.9.9", rel) == ""
    assert _render_digest("local/catena-admin:bench", rel) == ""
    assert _render_digest("local/catena-admin:bench", rel,
                          env_digest="sha256:" + "cd" * 32) == "sha256:" + "cd" * 32


def test_an_unresolved_release_extracts_unchecked_rather_than_failing_closed():
    """CATENA_ADMIN_IMAGE suppresses the registry call, so catena_admin_release
    is undefined on that path. The expression must render empty -- which
    reconcile/roles/payload reports out loud as unchecked -- rather than raising or
    matching an empty ref against an empty image."""
    assert _render_digest("local/catena-admin:bench", jinja2.Undefined()) == ""
    assert _render_digest("", jinja2.Undefined()) == ""


def test_the_resolved_digest_survives_the_swarm_pinned_ref():
    """reconcile/roles/payload follows the catena-admin service's image, and swarm stores
    that as repo:tag@sha256:... -- it resolves the tag at create/update time.
    The resolved ref is digest-pinned too, so BOTH sides carry a digest. A
    comparison of whole strings never matches, and every converged host would
    quietly drop to an unchecked extract while the gate still looked wired
    up."""
    tag_ref = "ghcr.io/catenahq/catena-admin:v0.3.1"
    dig = "sha256:" + "ab" * 32
    rel = _release(tag_ref, dig)
    assert _render_digest(tag_ref, rel) == dig
    assert _render_digest(f"{tag_ref}@{dig}", rel) == dig
    assert _render_digest(f"{tag_ref}@sha256:{'ef' * 32}", rel) == dig, (
        "the assertion must still be the RESOLVED digest, or a ref that "
        "carries its own digest would be trusted to verify itself"
    )


def _render_image(release=None, override="", pins=None):
    """Render catena_admin_image itself.

    The substring tests above cannot tell a correct expression from one that
    renders an empty string, and empty is the dangerous direction: every role
    downstream would pull "" or, worse, the filter would raise mid-converge.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "playbooks"
                           / "filter_plugins"))
    from image_pin import catena_image_pin  # noqa: PLC0415

    common = yaml.safe_load(
        (_role_dir("common") / "defaults" / "main.yml").read_text())
    env = jinja2.Environment()
    env.filters["catena_image_pin"] = catena_image_pin
    env.filters["ternary"] = lambda c, a, b: a if c else b
    return env.from_string(str(common["catena_admin_image"])).render(
        catena_admin_image_override=override,
        catena_admin_release=(jinja2.Undefined() if release is None else release),
        catena_image_pins=pins if pins is not None else {},
    ).strip()


def test_the_converge_installs_the_resolved_release_digest_and_all():
    rel = _release("ghcr.io/catenahq/catena-admin:v0.5.1", "sha256:" + "20" * 32)
    assert _render_image(release=rel) == rel["ref"]


def test_an_explicit_pin_beats_the_registry_answer():
    """CATENA_ADMIN_IMAGE is the change-control path, and the bench's. It has
    to win even when a release resolved, or a client who pinned would still
    move on the next converge."""
    rel = _release("ghcr.io/catenahq/catena-admin:v0.5.1", "sha256:" + "20" * 32)
    assert _render_image(release=rel,
                         override="local/catena-admin:bench") == "local/catena-admin:bench"


def test_a_newer_on_host_pin_still_wins_over_the_resolved_release():
    """The lane cannot bump past what is published, so in practice this ties.
    The wiring still has to be live: a converge that asserted its own answer
    here is what reverted every managed bump before the filter existed."""
    rel = _release("ghcr.io/catenahq/catena-admin:v0.5.1", "sha256:" + "20" * 32)
    pins = {"ghcr.io/catenahq/catena-admin": "ghcr.io/catenahq/catena-admin:v0.6.0"}
    assert _render_image(release=rel, pins=pins) == (
        "ghcr.io/catenahq/catena-admin:v0.6.0")


def test_a_rollback_recorded_on_this_host_survives_the_converge():
    """The panel's default is the NEWEST published release, re-resolved every
    converge, so max(default, pin) was always the default and the pin here could
    never win. A client who rolled their panel back to the version that worked
    was moved forward again by the next converge, silently, onto the build they
    had just rejected -- and a rollback that does not survive is not a rollback.

    The caller declares no minimum for exactly this reason. Everywhere else the
    converge ships the version, so the same literal is both the default and the
    oldest acceptable pin.
    """
    rel = _release("ghcr.io/catenahq/catena-admin:v0.6.2", "sha256:" + "20" * 32)
    pins = {"ghcr.io/catenahq/catena-admin": "ghcr.io/catenahq/catena-admin:v0.6.1"}
    assert _render_image(release=rel, pins=pins) == (
        "ghcr.io/catenahq/catena-admin:v0.6.1")


def test_the_override_is_not_something_a_pin_gets_to_argue_with():
    """CATENA_ADMIN_IMAGE is somebody saying exactly what to run, so it
    short-circuits ahead of the pin filter. Handed to that filter with
    everything else, a stored pin beats the one value whose purpose is change
    control -- silently, on the path a client uses when they want to decide when
    their panel moves."""
    rel = _release("ghcr.io/catenahq/catena-admin:v0.6.2", "sha256:" + "20" * 32)
    pins = {"ghcr.io/catenahq/catena-admin": "ghcr.io/catenahq/catena-admin:v0.6.2"}
    assert _render_image(
        release=rel, pins=pins,
        override="ghcr.io/catenahq/catena-admin:v0.6.0",
    ) == "ghcr.io/catenahq/catena-admin:v0.6.0"


def test_a_corrupt_pin_does_not_choose_the_panel_image():
    """No minimum is not no rules. The store is written through a dispatch
    action; a value it can name freely is a value that chooses what this host
    runs."""
    rel = _release("ghcr.io/catenahq/catena-admin:v0.6.2", "sha256:" + "20" * 32)
    for bad in ("ghcr.io/someone-else/panel:v9.9.9",
                "ghcr.io/catenahq/catena-admin:latest"):
        assert _render_image(
            release=rel,
            pins={"ghcr.io/catenahq/catena-admin": bad},
        ) == rel["ref"], bad


def test_the_engines_and_the_shell_come_from_one_image():
    """reconcile/roles/payload runs at 5.5 and reconcile/roles/catena-admin at 13, so neither can
    see the other's defaults. The value they share has to be declared in a role
    that runs before both, or the earlier one silently uses a fallback and the
    two agree only by coincidence of spelling."""
    common = yaml.safe_load(
        (_role_dir("common") / "defaults" / "main.yml").read_text())
    assert "catena_admin_image" in common
    assert "catena_admin_image_override" in common
    assert "catena_admin_service_name" in common, (
        "reconcile/roles/payload reads it at 5.5 to find the service whose image the "
        "engines follow; reconcile/roles/catena-admin's defaults are not in scope there")
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
        "are not in scope at role 5.5")


def test_the_converge_publishes_the_pins_before_any_role_reads_them():
    """The filter defaults to {} so a role cannot fail on a fresh host, which
    also means a loader that stopped publishing would be invisible: every
    image would silently resolve to its floor again."""
    loader = (Path(__file__).resolve().parents[2] / "playbooks" / "tasks"
              / "load_onbox_config.yml").read_text()
    assert "--emit image-pins" in loader
    assert "catena_image_pins" in loader


def test_the_loader_resolves_the_catena_admin_release_before_any_role():
    """reconcile/roles/payload reads the resolved release at role 5.5, so it has to be
    published in pre_tasks. The loader is included with apply: tags [always],
    which is what keeps a tag-scoped converge from running with no image at
    all -- the failure mode the vault_* facts already had."""
    tasks = Path(__file__).resolve().parents[2] / "playbooks" / "tasks"
    loader = (tasks / "load_onbox_config.yml").read_text()
    assert "catena_admin_release.py" in loader, (
        "the loader no longer resolves the catena-admin release; every role "
        "reading catena_admin_image would get an empty string")
    assert "catena_admin_release:" in loader
    assert "CATENA_ADMIN_IMAGE" in loader, (
        "the operator override must short-circuit the registry call, or an "
        "explicitly pinned host still fails when the registry is unreachable")
    for playbook in ("site.yml", "validate.yml", "restore.yml"):
        text = (tasks.parent / playbook).read_text()
        assert "load_onbox_config.yml" in text, (
            f"{playbook} does not load the on-box config, so it would run "
            "with no resolved catena-admin image")
