"""Ansible filters: resolve a service's image against the on-host pin.

Two writers move the images on a host: the on-host update lane, often and one
service at a time, and the converge, rarely and all at once. The lane records
what it applied in `image_pins` in /etc/catena/config.json, and a role reads
that answer through catena_image_pin, so a converge keeps every managed bump
rather than putting the shipped version back.

The first argument is the version the converge SHIPS, which is both what to
run when the host has no usable pin and the oldest version a pin may name, so
the result is max(floor, pin): pin-always would let a stale pin beat a
catena-ce release that raised a floor for a security fix, and floor-always
would revert every managed bump.

A pin is usable only when it names the repository it is keyed by and a
comparable tag (catena_image_pinned): a value that disagrees with its own key
is a corrupt store, and a store that could name an arbitrary string could
choose what this host runs. catena-ce/ansible/helpers/fetch_release.py reads
the catena-admin pin through the same rule. The floor wins ties and wins
whenever the two cannot be compared, so an unclear comparison never downgrades.

Two kinds of tag are comparable. A full semver tag compares with any other full
semver tag. A two-part `<major>.<minor>` tag, optionally with one suffix
(`18.7-trixie`), compares only with a two-part tag of the SAME major and the
SAME suffix, so a minor of the pinned major moves and a major, a variant or a
one-part floor (`postgres:18`) never does: a pin that appeared to beat those
could be a major upgrade nobody asked for.

The Go side writes pins and never resolves them, so this is the one
implementation of the comparison.

End-to-end coverage:
    catena-ce ansible/tests/unit/test_image_pin.py
"""
from __future__ import annotations

import re


# A full semver tag, optionally v-prefixed, optionally with a fourth numeric
# part (a build of that version: phasetwo-keycloak's 26.6.4.<unix-build-time>),
# optionally with a pre-release or build suffix. Anything else is incomparable
# on purpose.
_SEMVER = re.compile(
    r"^v?(\d+)\.(\d+)\.(\d+)(?:\.(\d+))?(?:[-+](.+))?$"
)


def _split_ref(ref):
    """(repository, tag) for an image reference, tag empty when there is none.

    A registry port is a colon too, so a colon with a slash after it is part of
    the repository rather than the start of a tag."""
    if not isinstance(ref, str) or not ref.strip():
        return "", ""
    ref = ref.strip()
    digest = ref.find("@")
    if digest >= 0:
        ref = ref[:digest]
    i = ref.rfind(":")
    if i < 0 or "/" in ref[i + 1:]:
        return ref, ""
    return ref[:i], ref[i + 1:]


# A two-part tag: major.minor with at most one alphabetic suffix.
_MAJOR_MINOR = re.compile(r"^(\d+)\.(\d+)(?:-([A-Za-z][A-Za-z0-9]*))?$")


def _order(tag):
    """(family, sortable key) for a comparable tag, or None when it is not one.

    Only tags of the same family compare. The semver family is every full
    semver tag; a two-part tag's family is its major and suffix.

    For semver, a pre-release sorts BELOW the same version without one, which
    is semver's own rule and the one that matters here: v3.8.0-rc1 must not
    beat v3.8.0. A build sorts above the plain version and below the next one,
    so a newer build of the running version survives the converge."""
    m = _MAJOR_MINOR.match(tag or "")
    if m:
        major, minor, suffix = m.groups()
        return ("mm", int(major), suffix or ""), (int(minor),)
    m = _SEMVER.match(tag or "")
    if not m:
        return None
    major, minor, patch, build, suffix = m.groups()
    return ("semver",), (int(major), int(minor), int(patch), int(build or 0),
                         0 if suffix else 1, suffix or "")


def catena_image_pinned(pins, repository):
    """The pin recorded for `repository`, or "" when there is no usable one.

    pins is the {repository: image_ref} map read from the on-box store. A
    usable pin names `repository` itself and a comparable tag."""
    if not isinstance(pins, dict):
        return ""
    pin_ref = pins.get(repository)
    if not isinstance(pin_ref, str) or not pin_ref.strip():
        return ""
    pin_repo, pin_tag = _split_ref(pin_ref)
    if pin_repo != repository or _order(pin_tag) is None:
        return ""
    return pin_ref


def catena_image_pin(default_ref, pins):
    """Return the image this host should run.

    default_ref is the shipped version: what to run when the host has no usable
    pin, and the oldest version a pin may name. pins is the
    {repository: image_ref} map read from the on-box store.

    Returns default_ref whenever there is no usable pin for its repository,
    default_ref is not a comparable version, or the pin is not newer than it."""
    if not isinstance(default_ref, str) or not default_ref.strip():
        raise ValueError(
            "catena_image_pin needs the image the converge would otherwise "
            f"pin, got {default_ref!r}"
        )
    repo, floor_tag = _split_ref(default_ref)
    pin_ref = catena_image_pinned(pins, repo)
    if not pin_ref:
        return default_ref

    floor_order = _order(floor_tag)
    if floor_order is None:
        return default_ref
    pin_family, pin_key = _order(_split_ref(pin_ref)[1])
    floor_family, floor_key = floor_order
    if pin_family != floor_family:
        return default_ref
    return pin_ref if pin_key > floor_key else default_ref


class FilterModule:
    def filters(self):
        return {
            "catena_image_pin": catena_image_pin,
            "catena_image_pinned": catena_image_pinned,
        }
