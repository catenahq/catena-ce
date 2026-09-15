"""Lock down the slimmed reconcile/roles/cloudflare_tunnel.

The tunnel/DNS/ingress/cloudflared-service converge belongs to the Go engine
catena-cloudflared-sync (host payload). The role dispatches that engine's `sync`
and prints what it says -- it does not decide whether there is a token, because
the engine reads the store and no-ops on its own. These tests pin that shape,
including the absence of any converge-time CF-API or swarm-service task.

Run: uv run pytest tests/unit/test_cloudflared_tunnel_dispatch.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

_ROLE = (
    Path(__file__).resolve().parents[3]
    / "ansible" / "reconcile" / "roles" / "cloudflare_tunnel"
)
TASKS = _ROLE / "tasks" / "main.yml"
DEFAULTS = _ROLE / "defaults" / "main.yml"


def _tasks() -> list[dict]:
    return yaml.safe_load(TASKS.read_text())


def _defaults() -> dict:
    return yaml.safe_load(DEFAULTS.read_text())


def _find(name_fragment: str) -> dict:
    for task in _tasks():
        if name_fragment in (task.get("name") or ""):
            return task
    raise AssertionError(f"task not found: {name_fragment!r}")


# --- the ported CF-API + swarm-service tasks are GONE -----------------------
def test_no_cloudflare_api_tasks_remain():
    """No ansible.builtin.uri calls -- the CF API find-or-create / DNS / ingress
    all live in the Go engine now."""
    for task in _tasks():
        assert "ansible.builtin.uri" not in task, (
            f"CF API task still in the role: {task.get('name')!r} -- that logic "
            "belongs to catena-cloudflared-sync now"
        )


def test_no_cloudflared_swarm_service_management():
    names = " ".join((t.get("name") or "") for t in _tasks()).lower()
    for gone in ("swarm service", "tunnel token", "wildcard dns",
                 "network reconcile", "network attachments"):
        assert gone not in names, f"{gone!r} task should be gone from the role"


# --- the role dispatches; the engine decides --------------------------------
def test_the_role_does_not_decide_whether_there_is_a_token():
    """One owner for the no-token call, and it is the engine.

    A gate here on _cf_token_present, computed from a fact
    tasks/load_onbox_config.yml publishes, is a second implementation of a
    question the engine answers from the store anyway -- and one that sees no
    token on a host that has one whenever a play skips the loader. The engine's
    own no-op (exit 0, "cloudflared-sync: skipped (no
    token)") is now the whole mechanism.
    """
    body = TASKS.read_text()
    assert "_cf_token_present" not in body
    for task in _tasks():
        fact = task.get("ansible.builtin.set_fact") or {}
        assert not any("cloudflare_api_token" in str(v) for v in fact.values())


def test_the_deferral_message_is_no_longer_ansible_s_to_write():
    names = " ".join((t.get("name") or "") for t in _tasks())
    assert "tunnel deferred -- no Cloudflare token" not in names


def test_dispatches_sync_engine_with_only_the_per_host_value():
    """The engine owns its own spec; this passes what it cannot know.

    The image, ingress service, network, stop grace and probe counts are the
    engine's own defaults and are not repeated here: a value in both places
    agrees only while somebody keeps it agreeing. Worse, exporting them makes
    the two dispatch paths structurally different -- the panel fires the same
    engine, and an engine that reads its caller's environment works from the
    caller written beside it and fails from every other one.

    ONE survives, and it is the one thing the engine cannot resolve: the zone,
    because an account-scoped token grants many. The tunnel NAME is not here
    either -- the engine composes it from the box's own hostname and the
    prefix in the store, so the panel path arrives at the same name with
    nothing rendered for it. That is what let the two cloudflared actions move
    into the image's dispatch drop-in.
    """
    task = _find("converge the tunnel via catena-cloudflared-sync")
    argv = task["ansible.builtin.command"]["argv"]
    assert argv[-1] == "sync"
    assert "cloudflared_sync_bin" in argv[0]

    env = task["environment"]
    assert set(env) == {"CLOUDFLARE_PRIMARY_ZONE"}, (
        f"the sync dispatch exports {sorted(env)}; anything beyond the one "
        "per-host value is either a second copy of a product constant the "
        "engine already carries, or a name the panel path cannot render"
    )
    # The token is NEVER passed to the engine (it reads the store).
    assert not any("cloudflare_api_token" in str(v) for v in env.values())


def test_sync_gated_on_the_binary_only():
    """A missing engine and a missing token are different failures. The first
    is worth stopping for; the second is the engine's own no-op."""
    task = _find("converge the tunnel via catena-cloudflared-sync")
    when = task["when"]
    cond = " ".join(when) if isinstance(when, list) else str(when)
    assert "_cf_sync_present" in cond
    assert "token" not in cond


def test_the_decision_comes_from_the_shared_predicate():
    """One include, one answer. This role and reconcile/roles/backup read the
    published fact while the validate side reads the env var, and a leg that
    accepts "or it is already here" on one side only leaves the other unable to
    tell NOT YET from PARTIALLY INSTALLED."""
    task = _find("is the tunnel engine expected on this host")
    assert task["ansible.builtin.include_role"]["tasks_from"] == "_payload_expected"
    assert task["vars"]["_payload_paths"] == ["{{ cloudflared_sync_bin }}"]


def test_a_missing_engine_fails_a_converge_that_installs_it():
    """reconcile/roles/payload puts the engine on the host four roles earlier. If
    it is not there, deferring only moves the failure to oauth2_proxy, which
    waits on an edge this role is the one to bring up."""
    task = _find("engine is missing from a converge that installs it")
    assert "ansible.builtin.fail" in task
    cond = " ".join(str(c) for c in task["when"])
    assert "catena_payload_expected" in cond
    assert "catena_payload_missing" in cond


def test_a_host_with_out_of_band_engines_still_defers():
    task = _find("engine staged out of band")
    cond = " ".join(str(c) for c in task["when"])
    assert "not (catena_payload_expected" in cond


def test_the_decision_is_pinned_before_the_next_include_overwrites_it():
    """_payload_expected sets play-level facts; reconcile/roles/backup includes it again
    later in the same play. Reading them after that would answer for backup."""
    pinned = _find("pin whether the tunnel engine is here")
    assert "_cf_sync_present" in pinned["ansible.builtin.set_fact"]


def test_sync_reports_unchanged_for_idempotency_gate():
    task = _find("converge the tunnel via catena-cloudflared-sync")
    assert task["changed_when"] is False


def test_no_licence_flag_is_passed_to_the_engine():
    """The engine reads the subscription out of the on-box store and decides the
    multi-domain cap itself. A boolean handed to it from here is a second,
    Python implementation of licence verification: it can disagree with the Go
    one, and it is verification logic a public repo has no business carrying."""
    task = _find("converge the tunnel via catena-cloudflared-sync")
    env = task["environment"]
    assert "CATENA_MULTIDOMAIN_ENABLED" not in env
    blob = " ".join(f"{k}={v}" for k, v in env.items()).lower()
    for gone in ("multidomain", "license", "licence", "catena_ee_"):
        assert gone not in blob, f"{gone!r} still reaches the engine from this role"


def test_no_licence_semantics_anywhere_in_the_role():
    """CP2: no Business logic in the public repo. The role must not mention a
    licence variable at all, in any task."""
    body = TASKS.read_text().lower()
    for gone in ("catena_ee_multidomain_enabled", "catena_license", "catena_licence"):
        assert gone not in body, f"{gone!r} is licence semantics in the public repo"


def test_le_warning_preserved():
    task = _find("engine summary")
    assert "DO NOT enable Let's Encrypt" in task["ansible.builtin.debug"]["msg"]


# --- defaults carry the engine bin and nothing the engine already owns -------
def test_defaults_expose_the_engine_bin():
    d = _defaults()
    assert d["cloudflared_sync_bin"] == "/usr/local/bin/catena-cloudflared-sync"


def test_defaults_no_longer_duplicate_the_engine_s_own_pins():
    """The image, ingress service, stop grace and probe counts are product
    constants the engine defaults to. Their VALUES are asserted in catena-admin
    (payload/cmd/catena-cloudflared-sync); keeping a copy here would make this
    repo a second writer of a value it does not pass any more, which is the
    worst of both -- editable, and read by nothing."""
    d = _defaults()
    for gone in ("cloudflared_image", "cloudflared_image_tag",
                 "cloudflared_ingress_service", "cloudflared_stop_grace",
                 "cloudflared_probe_attempts", "cloudflared_reprobe_attempts",
                 "cloudflared_probe_delay_seconds"):
        assert gone not in d, (
            f"{gone} is back in the role defaults; the engine already carries it"
        )
