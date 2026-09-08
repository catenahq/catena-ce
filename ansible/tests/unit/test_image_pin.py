"""The one comparison rule the two apply paths share.

The property under test is narrow and load-bearing: an on-host bump must
survive the next converge, and a floor raised by a catena-ce release must beat a
stale pin. Every other case resolves to the default, because "unclear" and
"downgrade" must not be the same answer.

The second half of the file is the panel's case, where the first argument is not
a floor at all -- it is the newest published release, resolved on every converge
-- so the caller declares no minimum and the host's recorded choice stands.
Without that, max(newest, pin) is newest and a rollback chosen in the panel is
undone by the next converge.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(
    0, str(Path(__file__).resolve().parents[2] / "playbooks" / "filter_plugins")
)

from image_pin import catena_image_pin  # noqa: E402


TRAEFIK_FLOOR = "traefik:v3.7.10"


def test_a_newer_pin_wins_which_is_the_whole_point():
    """Without this the converge puts the old image back and says nothing."""
    pins = {"traefik": "traefik:v3.7.12"}
    assert catena_image_pin(TRAEFIK_FLOOR, pins) == "traefik:v3.7.12"


def test_a_raised_floor_beats_a_stale_pin():
    """A catena-ce release raising a floor for a security fix must not lose to
    a pin written months ago."""
    pins = {"traefik": "traefik:v3.7.8"}
    assert catena_image_pin(TRAEFIK_FLOOR, pins) == TRAEFIK_FLOOR


def test_equal_versions_resolve_to_the_floor():
    pins = {"traefik": "traefik:v3.7.10"}
    assert catena_image_pin(TRAEFIK_FLOOR, pins) == TRAEFIK_FLOOR


def test_no_pins_at_all_is_the_fresh_host():
    for pins in ({}, None, []):
        assert catena_image_pin(TRAEFIK_FLOOR, pins) == TRAEFIK_FLOOR


def test_a_pin_for_another_service_is_not_applied():
    pins = {"portainer/portainer-ce": "portainer/portainer-ce:2.44.1"}
    assert catena_image_pin(TRAEFIK_FLOOR, pins) == TRAEFIK_FLOOR


def test_a_pin_whose_value_names_a_different_repository_is_ignored():
    """Keyed by repository, so a value disagreeing with its own key is a
    corrupt store. Applying it would swap the service's software entirely."""
    pins = {"traefik": "nginx:v1.99.0"}
    assert catena_image_pin(TRAEFIK_FLOOR, pins) == TRAEFIK_FLOOR


@pytest.mark.parametrize("floor,pin", [
    # postgres is pinned to a MAJOR on purpose: it follows Keycloak's support
    # matrix. A pin that appeared to beat it would be a major upgrade nobody
    # asked for, mid-converge, under a running database.
    ("postgres:18", "postgres:19"),
    ("postgres:18", "postgres:18.4"),
    # Non-version tags carry no ordering at all.
    ("traefik:latest", "traefik:v3.7.12"),
    ("traefik:v3.7.10", "traefik:latest"),
    ("traefik:v3.7.10", "traefik:main"),
])
def test_an_incomparable_pair_resolves_to_the_floor(floor, pin):
    repo = floor.rsplit(":", 1)[0]
    assert catena_image_pin(floor, {repo: pin}) == floor


def test_a_prerelease_does_not_beat_the_release_it_precedes():
    assert catena_image_pin("traefik:v3.8.0", {"traefik": "traefik:v3.8.0-rc1"}) \
        == "traefik:v3.8.0"
    assert catena_image_pin("traefik:v3.8.0-rc1", {"traefik": "traefik:v3.8.0"}) \
        == "traefik:v3.8.0"


def test_a_registry_port_is_not_read_as_a_tag():
    floor = "registry.example:5000/app:v1.0.0"
    pins = {"registry.example:5000/app": "registry.example:5000/app:v1.2.0"}
    assert catena_image_pin(floor, pins) == "registry.example:5000/app:v1.2.0"


def test_a_v_prefix_on_one_side_only_still_compares():
    assert catena_image_pin("app:1.2.3", {"app": "app:v1.2.4"}) == "app:v1.2.4"
    assert catena_image_pin("app:v1.2.4", {"app": "app:1.2.3"}) == "app:v1.2.4"


def test_an_empty_default_is_a_caller_bug_not_a_default():
    """Returning something plausible here would let a role template an image
    from an undefined variable and converge anyway."""
    for bad in ("", "   ", None, 3):
        with pytest.raises(ValueError):
            catena_image_pin(bad, {"traefik": "traefik:v3.7.12"})


# --- the panel: no minimum, because its default is the newest release --------

PANEL = "ghcr.io/catenahq/catena-admin"
NEWEST = PANEL + ":v0.6.2@sha256:" + "b" * 64
ROLLED_BACK = PANEL + ":v0.6.1@sha256:" + "a" * 64


def test_a_rollback_survives_the_next_converge():
    """The defect this argument exists for.

    The panel's own update lane records what it applied, rollbacks included. The
    first argument here is the newest PUBLISHED release, re-resolved from the
    registry on every converge, so max(default, pin) was always the default: a
    client who rolled back to the version that worked was moved forward again by
    the next converge, silently, onto the build they had just rejected.
    """
    assert catena_image_pin(NEWEST, {PANEL: ROLLED_BACK}, "") == ROLLED_BACK


def test_the_same_call_with_the_default_minimum_is_the_old_behaviour():
    """The one line that separates the two callers, shown as one line.

    Every shipped-version caller omits the minimum and keeps max(floor, pin)
    exactly as before; this is what they would have got."""
    assert catena_image_pin(NEWEST, {PANEL: ROLLED_BACK}) == NEWEST


def test_a_forward_pin_still_wins_with_no_minimum():
    ahead = PANEL + ":v0.7.0@sha256:" + "c" * 64
    assert catena_image_pin(NEWEST, {PANEL: ahead}, "") == ahead


def test_no_minimum_does_not_mean_no_rules():
    """A store that can name an arbitrary string is a store that can choose what
    this host runs."""
    for bad in ("ghcr.io/someone-else/panel:v9.9.9", PANEL + ":latest",
                PANEL + ":main", "", "   "):
        assert catena_image_pin(NEWEST, {PANEL: bad}, "") == NEWEST


def test_a_fresh_host_with_no_pin_gets_the_newest_release():
    assert catena_image_pin(NEWEST, {}, "") == NEWEST
