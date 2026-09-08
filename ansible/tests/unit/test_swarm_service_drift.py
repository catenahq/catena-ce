"""Unit tests for the swarm_service_create_args / swarm_service_drift
filters used by roles/postgres, roles/portainer and roles/traefik to render
and reconcile the swarm-service hardening flags.

The load-bearing property is that a converged service produces an EMPTY
drift list: the roles key `changed_when` off it, so a non-empty list on an
unchanged host would make every re-converge report changed and break the
bench idempotency gate.

The second property is that create and update render from the SAME desired
dict. A role that renders the two separately reconciles whatever it happens
to have taught the update path, so a flag reaches fresh installs only.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ANSIBLE_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ANSIBLE_DIR / "playbooks" / "filter_plugins"))

from swarm_service_drift import (  # noqa: E402
    FilterModule,
    swarm_service_create_args,
    swarm_service_drift,
)


GIB = 1024 ** 3
MIB = 1024 ** 2
SECOND_NS = 1_000_000_000


def _inspect(**overrides):
    """A `docker service inspect` payload matching the DESIRED spec below, so
    a test only has to override the one field it is exercising."""
    spec = {
        "TaskTemplate": {
            "ContainerSpec": {
                "Image": "postgres:18@sha256:" + "0" * 64,
                "StopGracePeriod": 60 * SECOND_NS,
                "Healthcheck": {
                    "Test": ["CMD-SHELL", "pg_isready -U postgres"],
                    "Interval": 10 * SECOND_NS,
                    "Retries": 5,
                    "StartPeriod": 60 * SECOND_NS,
                },
            },
            "Resources": {
                "Limits": {"MemoryBytes": 2 * GIB},
                "Reservations": {"MemoryBytes": 512 * MIB},
            },
            "Placement": {"Constraints": ["node.labels.catena.role==data"]},
        },
        "UpdateConfig": {
            "FailureAction": "pause",
            "Monitor": 60 * SECOND_NS,
            "MaxFailureRatio": 0,
            "Order": "stop-first",
        },
    }
    container = spec["TaskTemplate"]["ContainerSpec"]
    resources = spec["TaskTemplate"]["Resources"]
    for key, value in overrides.items():
        if key == "image":
            container["Image"] = value
        elif key == "stop_grace":
            container["StopGracePeriod"] = value
        elif key == "healthcheck":
            container["Healthcheck"] = value
        elif key == "limit":
            resources["Limits"] = {"MemoryBytes": value}
        elif key == "reservation":
            resources["Reservations"] = {"MemoryBytes": value}
        elif key == "constraints":
            spec["TaskTemplate"]["Placement"] = {"Constraints": value}
        elif key == "update":
            spec["UpdateConfig"] = value
        else:  # pragma: no cover - guards a typo in a future test
            raise KeyError(key)
    return [{"Spec": spec}]


DESIRED = {
    "image": "postgres:18",
    "limit_memory": "2g",
    "reserve_memory": "512m",
    "stop_grace_period": "60s",
    "update_failure_action": "pause",
    "update_monitor": "60s",
    "update_max_failure_ratio": 0,
    "update_order": "stop-first",
    "constraints": ["node.labels.catena.role==data"],
    "health_cmd": "pg_isready -U postgres",
    "health_interval": "10s",
    "health_retries": 5,
    "health_start_period": "60s",
}


# --- the idempotency property ---------------------------------------------

def test_converged_service_reports_no_drift():
    assert swarm_service_drift(_inspect(), DESIRED) == []


def test_resolved_digest_is_not_drift():
    """Swarm appends the digest it resolved to the image it pinned. Comparing
    the whole string would report drift against our own pinned tag forever."""
    payload = _inspect(image="postgres:18@sha256:" + "a" * 64)
    assert swarm_service_drift(payload, {"image": "postgres:18"}) == []


def test_a_digest_pinned_service_on_its_own_pin_reports_no_drift():
    """The shape every release host is actually in, and the one shape no test
    used to cover.

    catena_admin_image carries its digest -- catena_admin_release.ref is
    `<repo>:<tag>@sha256:...` -- so the comparison was `<repo>:<tag>` against
    `<repo>:<tag>@sha256:...` and never matched. Every converge emitted --image
    against its own pin. Docker no-ops an update to the identical spec, so
    nothing restarted and no client was harmed; what it cost was the ability to
    read "did this converge move my panel" out of the output, which matters more
    the moment a converge runs unattended on a timer.

    The tests passed because they all used a digest-less desired image, which is
    the one shape no release host is in.
    """
    pinned = "ghcr.io/catenahq/catena-admin:v0.6.2@sha256:" + "b" * 64
    assert swarm_service_drift(_inspect(image=pinned), {"image": pinned}) == []


def test_a_digest_pin_is_a_demand_for_those_exact_bytes():
    """Same tag, different bytes. Stripping the digest off both sides would be
    the tidy-looking fix and would answer "converged" here -- which is the one
    thing a digest pin exists to prevent."""
    repo = "ghcr.io/catenahq/catena-admin:v0.6.2@sha256:"
    drift = swarm_service_drift(_inspect(image=repo + "a" * 64),
                                {"image": repo + "b" * 64})
    assert drift[:2] == ["--image", repo + "b" * 64], drift


def test_accepts_unwrapped_dict_as_well_as_the_inspect_list():
    assert swarm_service_drift(_inspect()[0], DESIRED) == []


def test_missing_service_asks_for_everything():
    """An empty inspect (service absent) must not raise -- it yields the full
    flag set, which is what a caller reconciling a half-created service needs."""
    assert "--image" in swarm_service_drift([], DESIRED)


# --- per-field drift -------------------------------------------------------

def test_image_drift():
    payload = _inspect(image="postgres:17@sha256:" + "0" * 64)
    assert swarm_service_drift(payload, DESIRED)[:2] == ["--image", "postgres:18"]


def test_memory_limit_drift_is_binary_prefixed():
    """2g must compare equal to 2 GiB, not 2e9 -- docker's memory flags are
    go-units binary."""
    assert swarm_service_drift(_inspect(limit=2 * GIB), {"limit_memory": "2g"}) == []
    assert swarm_service_drift(
        _inspect(limit=2_000_000_000), {"limit_memory": "2g"}
    ) == ["--limit-memory", "2g"]


def test_stop_grace_drift():
    payload = _inspect(stop_grace=10 * SECOND_NS)
    assert swarm_service_drift(payload, {"stop_grace_period": "60s"}) == [
        "--stop-grace-period", "60s",
    ]


def test_update_policy_drift():
    payload = _inspect(update={
        "FailureAction": "pause", "Monitor": 5 * SECOND_NS,
        "MaxFailureRatio": 0, "Order": "start-first",
    })
    assert swarm_service_drift(payload, DESIRED) == [
        "--update-monitor", "60s", "--update-order", "stop-first",
    ]


def test_undeclared_keys_are_left_alone():
    """A field the desired dict does not mention is not reconciled, so a
    deliberate manual `docker service update` is not silently reverted."""
    payload = _inspect(limit=99 * MIB, stop_grace=1 * SECOND_NS)
    assert swarm_service_drift(payload, {"image": "postgres:18"}) == []


# --- constraints use add/rm, not set --------------------------------------

def test_constraint_added_when_missing():
    payload = _inspect(constraints=[])
    assert swarm_service_drift(payload, {"constraints": ["node.role==manager"]}) == [
        "--constraint-add", "node.role==manager",
    ]


def test_constraint_removed_when_dropped_from_desired():
    """A constraint deleted from the spec has to be REMOVED. Leaving it
    unmentioned keeps the service pinned to a node the spec does not name --
    which, once a worker exists, is how a stateful service ends up somewhere
    its volume is not."""
    payload = _inspect(constraints=["node.role==manager", "node.labels.x==y"])
    assert swarm_service_drift(payload, {"constraints": ["node.role==manager"]}) == [
        "--constraint-rm", "node.labels.x==y",
    ]


def test_constraint_swap_emits_rm_before_add():
    payload = _inspect(constraints=["node.labels.old==1"])
    assert swarm_service_drift(payload, {"constraints": ["node.labels.new==1"]}) == [
        "--constraint-rm", "node.labels.old==1",
        "--constraint-add", "node.labels.new==1",
    ]


# --- healthcheck is replaced wholesale ------------------------------------

def test_healthcheck_interval_drift_resends_every_field():
    """Docker replaces the whole healthcheck struct, so a partial update would
    reset the untouched fields to the image defaults."""
    payload = _inspect(healthcheck={
        "Test": ["CMD-SHELL", "pg_isready -U postgres"],
        "Interval": 30 * SECOND_NS, "Retries": 5,
        "StartPeriod": 60 * SECOND_NS,
    })
    assert swarm_service_drift(payload, DESIRED) == [
        "--health-cmd", "pg_isready -U postgres",
        "--health-interval", "10s",
        "--health-retries", "5",
        "--health-start-period", "60s",
    ]


def test_healthcheck_absent_on_service_is_drift():
    payload = _inspect(healthcheck={})
    assert swarm_service_drift(payload, DESIRED)[:2] == [
        "--health-cmd", "pg_isready -U postgres",
    ]


# --- create renders from the same dict ------------------------------------

def test_create_args_render_every_declared_field():
    args = swarm_service_create_args(DESIRED)
    assert args == [
        "--limit-memory", "2g",
        "--reserve-memory", "512m",
        "--stop-grace-period", "60s",
        "--update-failure-action", "pause",
        "--update-monitor", "60s",
        "--update-max-failure-ratio", "0",
        "--update-order", "stop-first",
        "--constraint", "node.labels.catena.role==data",
        "--health-cmd", "pg_isready -U postgres",
        "--health-interval", "10s",
        "--health-retries", "5",
        "--health-start-period", "60s",
    ]


def test_create_args_omit_the_image_flag():
    """`docker service create` takes the image as a positional, not --image.
    The role appends it after these flags."""
    assert "--image" not in swarm_service_create_args(DESIRED)


def test_create_args_repeat_constraint_flag():
    args = swarm_service_create_args(
        {"constraints": ["node.role==manager", "node.labels.catena.role==data"]}
    )
    assert args == [
        "--constraint", "node.role==manager",
        "--constraint", "node.labels.catena.role==data",
    ]


def test_create_and_drift_agree_on_a_service_built_from_create_args():
    """The two renderers must not disagree: a service created from
    create_args, then inspected, must show no drift."""
    assert swarm_service_create_args(DESIRED)
    assert swarm_service_drift(_inspect(), DESIRED) == []


# --- parsing edges ---------------------------------------------------------

@pytest.mark.parametrize("value,expected_ns", [
    ("40s", 40 * SECOND_NS),
    ("1m", 60 * SECOND_NS),
    ("1m30s", 90 * SECOND_NS),
    ("500ms", 500_000_000),
    ("2h", 7200 * SECOND_NS),
])
def test_duration_forms_compare_equal(value, expected_ns):
    payload = _inspect(stop_grace=expected_ns)
    assert swarm_service_drift(payload, {"stop_grace_period": value}) == []


@pytest.mark.parametrize("bad", ["", "  ", "60", "40x", None, True])
def test_bad_duration_raises(bad):
    with pytest.raises(ValueError):
        swarm_service_drift(_inspect(), {"stop_grace_period": bad})


@pytest.mark.parametrize("bad", ["", "  ", "12x", None, True])
def test_bad_memory_raises(bad):
    with pytest.raises(ValueError):
        swarm_service_drift(_inspect(), {"limit_memory": bad})


def test_non_dict_desired_raises():
    with pytest.raises(ValueError):
        swarm_service_drift(_inspect(), ["not", "a", "dict"])
    with pytest.raises(ValueError):
        swarm_service_create_args("nope")


def test_filter_module_exposes_both_filters():
    assert set(FilterModule().filters()) == {
        "swarm_service_create_args",
        "swarm_service_drift",
    }
