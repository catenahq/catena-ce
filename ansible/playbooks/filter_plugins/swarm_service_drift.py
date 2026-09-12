"""Ansible filters: render swarm-service hardening flags, and diff them
against a live service.

Two filters over ONE desired-spec dict:

    swarm_service_create_args(desired)   -> flags for `docker service create`
    swarm_service_drift(inspect, desired) -> flags for `docker service update`,
                                             empty list when already converged

One dict feeding two renderers is what keeps a fresh service and a converged
one identical. A role that renders create and update separately has two
definitions of the same spec to keep in step, and they drift.

SCOPE. These filters own the HARDENING subset only: image, memory
limit/reservation, stop-grace, update policy, placement constraints and
healthcheck. Identity and wiring (--name, --network, --secret, --mount,
--env, --publish, --restart-condition) stay in the role's own create argv:
they are either create-only or need add/rm semantics that nothing here
needs yet.

Keys ABSENT from `desired` are not reconciled. A filter that forced every
unmentioned field would fight an operator who ran `docker service update`
by hand, and would silently revert a deliberate change. Declared means
managed; undeclared means left alone.

Note on update policy: `update_failure_action` must be `pause`, never
`rollback`. The managed-update engine (catena-admin
payload/engines/stackupdate) is the rollback authority -- it decides on a
whole-host health diff, keeps a persistent quarantine, and replays a DB
snapshot. If swarm rolls back underneath it, the engine either records a
version that never shipped as successfully bumped and retries it forever,
or takes its safety-net branch and re-applies the bad image, because
`docker service update --rollback` reverts to the PREVIOUS spec -- which,
after an auto-rollback, is the failed one. `pause` halts the rollout
against a still-good spec and lets the engine do its job.

End-to-end coverage:
    catena-ce ansible/tests/unit/test_swarm_service_drift.py
"""

from __future__ import annotations


_BYTE_SUFFIXES = {
    "b": 1,
    "k": 1024,
    "m": 1024 ** 2,
    "g": 1024 ** 3,
    "t": 1024 ** 4,
}

# Go duration units, in nanoseconds. Longest suffix first so "ms" is matched
# before "m" when scanning.
_DURATION_UNITS = (
    ("ns", 1),
    ("us", 1_000),
    ("ms", 1_000_000),
    ("s", 1_000_000_000),
    ("m", 60 * 1_000_000_000),
    ("h", 3600 * 1_000_000_000),
)


def _as_bytes(value):
    """Parse a docker memory value ('512m', '2g', 1048576) to an int of bytes.

    Docker's memory flags are binary-prefixed (512m == 512 MiB), matching
    go-units, so 1024 is the multiplier and not 1000."""
    if isinstance(value, bool):
        raise ValueError("swarm_service_drift: memory value must not be a bool")
    if isinstance(value, int):
        return value
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"swarm_service_drift: bad memory value {value!r}")
    text = value.strip().lower().rstrip("ib")
    if not text:
        raise ValueError(f"swarm_service_drift: bad memory value {value!r}")
    multiplier = 1
    if text[-1] in _BYTE_SUFFIXES:
        multiplier = _BYTE_SUFFIXES[text[-1]]
        text = text[:-1]
    try:
        magnitude = float(text)
    except ValueError:
        raise ValueError(f"swarm_service_drift: bad memory value {value!r}") from None
    return int(magnitude * multiplier)


def _as_nanos(value):
    """Parse a Go duration ('40s', '1m30s', 500000000) to an int of nanoseconds.

    `docker service inspect` reports every duration in nanoseconds while the
    CLI flags take duration strings, so a comparison needs one or the other.
    Nanoseconds is the lossless side."""
    if isinstance(value, bool):
        raise ValueError("swarm_service_drift: duration must not be a bool")
    if isinstance(value, int):
        return value
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"swarm_service_drift: bad duration {value!r}")
    text = value.strip().lower()
    total = 0
    number = ""
    index = 0
    while index < len(text):
        char = text[index]
        if char.isdigit() or char == ".":
            number += char
            index += 1
            continue
        for suffix, scale in _DURATION_UNITS:
            if text.startswith(suffix, index):
                if not number:
                    raise ValueError(
                        f"swarm_service_drift: bad duration {value!r}")
                total += int(float(number) * scale)
                number = ""
                index += len(suffix)
                break
        else:
            raise ValueError(f"swarm_service_drift: bad duration {value!r}")
    if number:
        raise ValueError(
            f"swarm_service_drift: duration {value!r} has no unit suffix")
    return total


def _strip_digest(image_ref):
    """`docker service inspect` appends the resolved `@sha256:...` digest to
    the image it pinned. Compare on the `<repo>:<tag>` half only, or every
    converge would see drift against its own pinned tag."""
    if not isinstance(image_ref, str):
        return ""
    return image_ref.split("@", 1)[0]


def _same_image(live, want):
    """Is the running service already on the wanted image?

    Asymmetric, in the one direction that is correct. `docker service inspect`
    reports the digest it resolved at deploy time, whether or not the reference
    it receives carries one -- so what the comparison does depends on what the
    PIN says, not on what the host reports:

      - a pin with no digest (`postgres:18`) is compared against the live
        `<repo>:<tag>` half, or every converge sees drift against its own tag;
      - a pin WITH one (`ghcr.io/...:v1.2.3@sha256:...`, which is what the
        panel's release resolution produces) is a demand for those exact bytes,
        so the live reference has to match it whole.

    Stripping the digest off both sides would be the tidy-looking version of
    this and it would be wrong twice over: it answers "converged" for a host
    running different bytes under the same tag, which is the one thing a digest
    pin exists to prevent. Stripping it from the live side alone compares
    `<repo>:<tag>` against `<repo>:<tag>@sha256:...`, which never matches, so a
    digest pin reports drift against itself on every converge. Docker no-ops an
    update to an identical spec, so nothing restarts; the cost is the output's
    ability to answer "did this converge move my panel", which matters most when
    a converge runs unattended on a timer.
    """
    want = str(want)
    if "@" in want:
        return str(live) == want
    return _strip_digest(live) == want


def _health_test(health_cmd):
    """The Test array docker builds for `--health-cmd X`."""
    return ["CMD-SHELL", health_cmd]


def _first(inspect):
    """`docker service inspect` returns a one-element list. Accept either that
    or an already-unwrapped dict, so a caller can pipe `from_json` straight in."""
    if isinstance(inspect, list):
        return inspect[0] if inspect else {}
    if isinstance(inspect, dict):
        return inspect
    return {}


def swarm_service_create_args(desired):
    """Render the hardening flags for `docker service create`.

    Returns a flat list the role concatenates between its own identity flags
    and the image ref. Absent keys emit nothing."""
    if not isinstance(desired, dict):
        raise ValueError("swarm_service_create_args: desired must be a dict")
    args: list[str] = []

    if "limit_memory" in desired:
        args += ["--limit-memory", str(desired["limit_memory"])]
    if "reserve_memory" in desired:
        args += ["--reserve-memory", str(desired["reserve_memory"])]
    if "stop_grace_period" in desired:
        args += ["--stop-grace-period", str(desired["stop_grace_period"])]

    if "update_failure_action" in desired:
        args += ["--update-failure-action", str(desired["update_failure_action"])]
    if "update_monitor" in desired:
        args += ["--update-monitor", str(desired["update_monitor"])]
    if "update_max_failure_ratio" in desired:
        args += ["--update-max-failure-ratio",
                 str(desired["update_max_failure_ratio"])]
    if "update_order" in desired:
        args += ["--update-order", str(desired["update_order"])]

    for constraint in desired.get("constraints", []):
        args += ["--constraint", str(constraint)]

    if "health_cmd" in desired:
        args += ["--health-cmd", str(desired["health_cmd"])]
        if "health_interval" in desired:
            args += ["--health-interval", str(desired["health_interval"])]
        if "health_retries" in desired:
            args += ["--health-retries", str(desired["health_retries"])]
        if "health_start_period" in desired:
            args += ["--health-start-period", str(desired["health_start_period"])]

    return args


def swarm_service_drift(inspect, desired):
    """Diff a live service against `desired` and return the
    `docker service update` flags that close the gap.

    An empty list means converged, which is what the role's `changed_when`
    keys off -- so a re-converge with nothing to change touches nothing and
    reports unchanged."""
    if not isinstance(desired, dict):
        raise ValueError("swarm_service_drift: desired must be a dict")
    spec = _first(inspect).get("Spec", {}) or {}
    task = spec.get("TaskTemplate", {}) or {}
    container = task.get("ContainerSpec", {}) or {}
    resources = task.get("Resources", {}) or {}
    update = spec.get("UpdateConfig", {}) or {}
    placement = task.get("Placement", {}) or {}

    args: list[str] = []

    if "image" in desired:
        if not _same_image(container.get("Image", ""), desired["image"]):
            args += ["--image", str(desired["image"])]

    limits = resources.get("Limits", {}) or {}
    if "limit_memory" in desired:
        if limits.get("MemoryBytes", 0) != _as_bytes(desired["limit_memory"]):
            args += ["--limit-memory", str(desired["limit_memory"])]

    reservations = resources.get("Reservations", {}) or {}
    if "reserve_memory" in desired:
        if reservations.get("MemoryBytes", 0) != _as_bytes(desired["reserve_memory"]):
            args += ["--reserve-memory", str(desired["reserve_memory"])]

    if "stop_grace_period" in desired:
        if container.get("StopGracePeriod", 0) != _as_nanos(
                desired["stop_grace_period"]):
            args += ["--stop-grace-period", str(desired["stop_grace_period"])]

    if "update_failure_action" in desired:
        if update.get("FailureAction", "") != desired["update_failure_action"]:
            args += ["--update-failure-action",
                     str(desired["update_failure_action"])]
    if "update_monitor" in desired:
        if update.get("Monitor", 0) != _as_nanos(desired["update_monitor"]):
            args += ["--update-monitor", str(desired["update_monitor"])]
    if "update_max_failure_ratio" in desired:
        if float(update.get("MaxFailureRatio", 0)) != float(
                desired["update_max_failure_ratio"]):
            args += ["--update-max-failure-ratio",
                     str(desired["update_max_failure_ratio"])]
    if "update_order" in desired:
        if update.get("Order", "") != desired["update_order"]:
            args += ["--update-order", str(desired["update_order"])]

    # Constraints take add/rm rather than a set, so reconcile both directions:
    # a constraint dropped from the desired list has to be REMOVED, not merely
    # left unmentioned, or the service stays pinned to a node the spec no
    # longer names.
    if "constraints" in desired:
        live = set(placement.get("Constraints") or [])
        want = {str(c) for c in desired["constraints"]}
        for constraint in sorted(live - want):
            args += ["--constraint-rm", constraint]
        for constraint in sorted(want - live):
            args += ["--constraint-add", constraint]

    if "health_cmd" in desired:
        health = container.get("Healthcheck", {}) or {}
        want_test = _health_test(str(desired["health_cmd"]))
        health_changed = (health.get("Test") or []) != want_test
        if not health_changed and "health_interval" in desired:
            health_changed = health.get("Interval", 0) != _as_nanos(
                desired["health_interval"])
        if not health_changed and "health_retries" in desired:
            health_changed = health.get("Retries", 0) != int(
                desired["health_retries"])
        if not health_changed and "health_start_period" in desired:
            health_changed = health.get("StartPeriod", 0) != _as_nanos(
                desired["health_start_period"])
        if health_changed:
            # Docker replaces the whole healthcheck, so resend every field
            # rather than only the one that differs -- a partial update would
            # reset the others to their image defaults.
            args += ["--health-cmd", str(desired["health_cmd"])]
            if "health_interval" in desired:
                args += ["--health-interval", str(desired["health_interval"])]
            if "health_retries" in desired:
                args += ["--health-retries", str(desired["health_retries"])]
            if "health_start_period" in desired:
                args += ["--health-start-period",
                         str(desired["health_start_period"])]

    return args


class FilterModule:
    def filters(self):
        return {
            "swarm_service_create_args": swarm_service_create_args,
            "swarm_service_drift": swarm_service_drift,
        }
