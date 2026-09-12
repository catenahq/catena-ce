"""Ansible filter: resolve a service's image against the on-host pin.

THE INVERSION THIS EXISTS FOR. Two systems change the images on a host: the
on-host update lane, frequently and one service at a time, and the converge,
rarely and all at once. They did not agree on anything. The lane bumped
catena-traefik; the next converge asked the catalog for traefik's image, got the
shipped floor, and emitted `--image` to put it back. Every managed bump was
reverted by the next converge, and nothing reported it -- the service came back
green on an older image.

So the converge stops asserting a version and starts asking for one. The lane
records what it successfully applied in `image_pins` in /etc/catena/config.json;
this filter is how a role reads that answer.

TWO DIFFERENT QUESTIONS, WHICH THIS USED TO CONFLATE. The first argument was
called the floor and did two jobs at once: what to run when the host has no
opinion, and the oldest version a pin may name. For everything the converge
SHIPS a version for, one literal answers both and the resolution is max(floor,
pin):

  - pin-always would mean a catena-ce release that raises a floor for a security
    fix loses to a stale pin, silently.
  - floor-always is the revert this whole thing exists to stop.

For the PANEL it is wrong, and wrong in a way that made the pin inert. Its
first argument is `catena_admin_release.ref` -- resolved from the registry on
every converge as the NEWEST published release. max(newest, pin) is newest, so
the pin could never win, and a client who deliberately rolled the panel back to
the version that worked had that rollback undone by the next converge, silently,
which is the same defect as the traefik one in the other direction. A rollback
that does not survive is not a rollback.

So the two jobs are separate arguments. `minimum` defaults to `default_ref`,
which keeps every shipped-version caller unchanged; the panel passes an
empty one, meaning "this host's recorded choice stands". The pin still has to
name the same repository and still has to be a comparable version -- a corrupt
store does not get to choose an image.

The minimum wins ties and wins whenever the two cannot be compared, because
"unclear" and "downgrade" must not be the same answer. A partial tag like
`postgres:18` is deliberately incomparable: it names a major, has no patch to
move within, and a pin that appeared to beat it would be a major upgrade nobody
asked for.

A pin for a DIFFERENT repository than the floor is ignored rather than applied.
The pins are keyed by repository so no mapping table is needed, and a value that
disagrees with its own key is a corrupt store, not an instruction.

This is the ONLY implementation of the comparison. The Go side writes pins and
never resolves them, so there is no second rule to keep in step.

End-to-end coverage:
    catena-ce ansible/tests/unit/test_image_pin.py
"""
from __future__ import annotations

import re


# A full semver tag, optionally v-prefixed, optionally with a pre-release or
# build suffix. Anything else is incomparable on purpose.
_SEMVER = re.compile(
    r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+](.+))?$"
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


def _order(tag):
    """A sortable key for a full-semver tag, or None when it is not one.

    A pre-release sorts BELOW the same version without one, which is semver's
    own rule and the one that matters here: v3.8.0-rc1 must not beat v3.8.0."""
    m = _SEMVER.match(tag or "")
    if not m:
        return None
    major, minor, patch, suffix = m.groups()
    return (int(major), int(minor), int(patch), 0 if suffix else 1, suffix or "")


def catena_image_pin(default_ref, pins, minimum=None):
    """Return the image this host should run.

    default_ref is what to run when the host has no usable pin. pins is the
    {repository: image_ref} map read from the on-box store.

    minimum is the oldest version a pin may name, and it DEFAULTS TO
    default_ref -- which is the right answer wherever the converge ships the
    version, and makes this max(floor, pin) exactly as before. Pass "" where
    default_ref is not a floor at all: the panel's is the newest published
    release, so using it as one leaves the pin unable to ever win and a
    deliberate rollback undone by the next converge.

    Returns default_ref whenever there is no pin for its repository, the pin
    names another repository, the pin is not a comparable version, or the pin is
    older than the minimum."""
    if not isinstance(default_ref, str) or not default_ref.strip():
        raise ValueError(
            "catena_image_pin needs the image the converge would otherwise "
            f"pin, got {default_ref!r}"
        )
    if not isinstance(pins, dict) or not pins:
        return default_ref

    repo, _ = _split_ref(default_ref)
    pin_ref = pins.get(repo)
    if not isinstance(pin_ref, str) or not pin_ref.strip():
        return default_ref

    pin_repo, pin_tag = _split_ref(pin_ref)
    if pin_repo != repo:
        # Keyed by repository, so a value naming another one is a corrupt
        # store. Ignoring it keeps the converge deterministic; applying it
        # would swap a service's software for something else entirely.
        return default_ref

    pin_order = _order(pin_tag)
    if pin_order is None:
        # Checked even with no minimum. A store that can name an arbitrary
        # string is a store that can choose what this host runs; requiring a
        # version keeps a corrupt one from doing it.
        return default_ref

    floor_ref = default_ref if minimum is None else minimum
    if not isinstance(floor_ref, str) or not floor_ref.strip():
        # No floor declared: this host's recorded choice stands, which is what
        # makes a rollback a rollback.
        return pin_ref

    _, floor_tag = _split_ref(floor_ref)
    floor_order = _order(floor_tag)
    if floor_order is None:
        return default_ref
    return pin_ref if pin_order > floor_order else default_ref


class FilterModule:
    def filters(self):
        return {"catena_image_pin": catena_image_pin}
