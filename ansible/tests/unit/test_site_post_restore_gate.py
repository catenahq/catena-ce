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
