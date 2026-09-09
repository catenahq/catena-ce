"""A tag-scoped converge must not select a task whose producer it filters out.

site.yml's post_tasks register `_site_post_restore` on the hook-runner task
and then read it from consumers. Ansible evaluates `when` only for tasks the
tag filter SELECTED, and a task the filter dropped registers nothing -- so a
consumer reachable under a tag set that does not also reach the producer sees
an UNDEFINED name, and `is not skipped` on an undefined name RAISES rather
than evaluating false.

That is not hypothetical. The post-restore coturn re-run was tagged
`[post_restore, coturn]` so a coturn-scoped converge could reach it, while
the hook-runner is tagged `[post_restore]` alone. Every
`--tags infrastructure,coturn` converge then died with:

    A 'when' expression failed: '_site_post_restore' is undefined

which is the bench's stage-4b wire-post-deploy, and every operator converge
scoped to coturn.

The invariant, checked structurally so a NEW consumer cannot reintroduce it:
a consumer of `_site_post_restore` either carries no tag beyond the
producer's (so it can never be selected without it), or guards its `when`
with `is defined`.

Run: uv run pytest tests/unit/test_site_post_restore_gate.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

SITE = (
    Path(__file__).resolve().parents[2] / "playbooks" / "site.yml"
)
REGISTER = "_site_post_restore"


def _post_tasks() -> list[dict]:
    plays = yaml.safe_load(SITE.read_text(encoding="utf-8"))
    for play in plays:
        if play.get("post_tasks"):
            return play["post_tasks"]
    raise AssertionError("site.yml has no play with post_tasks")


def _tags(task: dict) -> set[str]:
    raw = task.get("tags") or []
    return {raw} if isinstance(raw, str) else set(raw)


def _when_text(task: dict) -> str:
    when = task.get("when")
    if when is None:
        return ""
    return " and ".join(when) if isinstance(when, list) else str(when)


def _producer() -> dict:
    for task in _post_tasks():
        if task.get("register") == REGISTER:
            return task
    raise AssertionError(f"no post_task registers {REGISTER}")


def _consumers() -> list[dict]:
    return [
        t for t in _post_tasks()
        if t.get("register") != REGISTER and REGISTER in _when_text(t)
    ]


def test_there_is_exactly_one_producer():
    # Two tasks registering the same name would make "which one ran?"
    # unanswerable from the tag set alone, and this whole check moot.
    assert sum(
        1 for t in _post_tasks() if t.get("register") == REGISTER
    ) == 1


def test_every_consumer_is_reachable_only_with_its_producer():
    producer_tags = _tags(_producer())
    consumers = _consumers()
    assert consumers, "expected at least one gated consumer to check"
    for task in consumers:
        extra = _tags(task) - producer_tags
        if not extra:
            continue
        assert "is defined" in _when_text(task), (
            f"{task['name']!r} is selectable by {sorted(extra)}, which does "
            f"not select the task registering {REGISTER}; its `when` must "
            f"carry an `is defined` arm or the converge raises on an "
            f"undefined name"
        )


def test_the_coturn_rerun_applies_both_tags_to_the_included_role():
    """`apply:` decides the tags of the tasks INSIDE the include. With only
    `coturn` there, a --tags post_restore converge fires the include and then
    filters out every task in it -- a re-run that reports success and does
    nothing."""
    task = next(
        t for t in _post_tasks()
        if "consumer-gated companions" in (t.get("name") or "")
    )
    applied = task["ansible.builtin.include_role"]["apply"]["tags"]
    assert set(applied) == _tags(task)


def test_the_rerun_waits_for_a_redeployed_consumer_to_reach_running():
    """The hooks return when the stacks are DEPLOYED, not when their containers
    are up, and the companion gate reads `status=running`. Re-running the gate
    straight after them fixed the ordering and left a race: bench
    2026-08-03T21-33-27-7a33 ended the converge with no relay, and validate then
    found nextcloud-talk-hpb-1 "Up 51 seconds" -- it had come up in the gap.

    The wait must sit BETWEEN the hooks and the re-run, and must be bounded."""
    names = [t.get("name") or "" for t in _post_tasks()]
    probe = next(i for i, n in enumerate(names) if "TURN consumer deployed at all" in n)
    wait = next(i for i, n in enumerate(names) if "reach running" in n)
    rerun = next(i for i, n in enumerate(names) if "consumer-gated companions" in n)
    assert probe < wait < rerun, (
        "the wait does not sit between the existence probe and the companion "
        f"re-run (probe={probe}, wait={wait}, rerun={rerun})"
    )

    task = _post_tasks()[wait]
    assert task["until"], "the wait has no until condition, so it is not a wait"
    assert int(task["retries"]) > 0 and int(task["delay"]) > 0
    assert task["failed_when"] is False, (
        "exhausting the window must not fail the converge -- a genuinely broken "
        "consumer is reconcile/roles/coturn's gate to report by skipping"
    )


def test_the_wait_is_skipped_when_no_consumer_is_deployed():
    """A restored host with no real-time app must pay nothing. The existence
    probe uses `docker ps -a` precisely so a still-starting container counts,
    which the running-only probe cannot answer -- that is what is being waited
    on."""
    names = [t.get("name") or "" for t in _post_tasks()]
    probe = _post_tasks()[next(i for i, n in enumerate(names)
                               if "TURN consumer deployed at all" in n)]
    assert "docker ps -a" in probe["ansible.builtin.shell"]
    assert "status=running" not in probe["ansible.builtin.shell"], (
        "the existence probe filters on running, so it answers the same "
        "question as the wait and the wait can never help"
    )

    wait = _post_tasks()[next(i for i, n in enumerate(names) if "reach running" in n)]
    cond = " ".join(str(c) for c in wait["when"])
    assert "_site_turn_any" in cond and "length) > 0" in cond
