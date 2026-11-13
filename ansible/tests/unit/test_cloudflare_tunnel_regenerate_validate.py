"""cloudflare_tunnel_regenerate validates that a rotation left one tunnel.

The role lists this host's tunnels by name before it deletes them, so a
rotation leaves the host one tunnel. Its validate asks the Cloudflare API the
same question through the same two task files main.yml includes, and asserts
the count: at most one always, exactly one while the edge is up. The lookup
waits for a token and a zone in the store.

Run: uv run pytest tests/unit/test_cloudflare_tunnel_regenerate_validate.py
"""
from __future__ import annotations

import yaml

from ansible_tree import role_dir

TASKS = role_dir("cloudflare_tunnel_regenerate") / "tasks"


def _load(name: str) -> list[dict]:
    return yaml.safe_load((TASKS / name).read_text(encoding="utf-8"))


def _includes(name: str) -> list[str]:
    return [t["ansible.builtin.include_tasks"] for t in _load(name)
            if "ansible.builtin.include_tasks" in t]


def _when(task: dict) -> str:
    when = task.get("when")
    return " and ".join(when) if isinstance(when, list) else str(when)


def _asserts(name: str) -> list[dict]:
    return [t for t in _load(name) if "ansible.builtin.assert" in t]


def test_validate_and_main_share_the_store_read_and_the_lookup():
    assert _includes("validate.yml") == ["_read_store.yml", "_list_tunnels.yml"]
    assert _includes("main.yml") == ["_read_store.yml", "_list_tunnels.yml"]


def test_the_api_calls_exist_once_and_the_validate_makes_none_of_its_own():
    assert not [t for t in _load("validate.yml") if "ansible.builtin.uri" in t]
    assert not [t for t in _load("main.yml")
                if "ansible.builtin.uri" in t
                and "?cascade=true" not in t["ansible.builtin.uri"]["url"]]


def test_the_lookup_waits_for_a_token_and_a_zone():
    tasks = _load("validate.yml")
    lookup = next(t for t in tasks if t.get("ansible.builtin.include_tasks") == "_list_tunnels.yml")
    deferred = next(t for t in tasks if "deferred" in t.get("name", ""))
    for t in (lookup, *_asserts("validate.yml")):
        assert "_cf_regen_api_token" in _when(t), t["name"]
        assert "_cf_regen_zone" in _when(t), t["name"]
    assert "_cf_regen_api_token" in _when(deferred)
    assert "_cf_regen_zone" in _when(deferred)


def test_the_validate_play_never_sets_the_zone_fact():
    """A set_fact of cloudflare_zone would freeze over the loader's value for
    every later role and for Vantage 2. Only main.yml publishes it, for the
    handoff in a play that loads no store."""
    def sets_zone(name: str) -> bool:
        return any("cloudflare_zone" in (t.get("ansible.builtin.set_fact") or {})
                   for t in _load(name))
    assert not sets_zone("_read_store.yml")
    assert not sets_zone("validate.yml")
    main = _load("main.yml")
    read = next(i for i, t in enumerate(main)
                if t.get("ansible.builtin.include_tasks") == "_read_store.yml")
    publish = main[read + 1]
    assert publish["ansible.builtin.set_fact"] == {"cloudflare_zone": "{{ _cf_regen_zone }}"}
    assert publish["no_log"] is True


def test_at_most_one_tunnel_and_exactly_one_while_the_edge_is_up():
    at_most, exactly = _asserts("validate.yml")
    assert at_most["ansible.builtin.assert"]["that"] == ["_cf_regen_list.json.result | length <= 1"]
    assert exactly["ansible.builtin.assert"]["that"] == ["_cf_regen_list.json.result | length == 1"]
    assert "_cf_edge_up | default(true)" in _when(exactly)
    assert "_cf_edge_up" not in _when(at_most)


def test_the_failure_names_the_tunnels_and_never_the_token():
    for t in _asserts("validate.yml"):
        msg = t["ansible.builtin.assert"]["fail_msg"]
        assert "t.id" in msg and "t.created_at" in msg
        assert "_cf_regen_api_token" not in msg


def test_main_still_refuses_before_any_api_call_when_the_store_has_no_token():
    tasks = _load("main.yml")
    names = [t.get("name", "") for t in tasks]
    refuse = next(i for i, n in enumerate(names) if "refuse when the store holds no Cloudflare token" in n)
    lookup = next(i for i, t in enumerate(tasks) if t.get("ansible.builtin.include_tasks") == "_list_tunnels.yml")
    read = next(i for i, t in enumerate(tasks) if t.get("ansible.builtin.include_tasks") == "_read_store.yml")
    assert read < refuse < lookup
    assert not [t for t in tasks[:lookup] if "ansible.builtin.uri" in t]
