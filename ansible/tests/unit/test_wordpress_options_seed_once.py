"""WordPress plugin OPTIONS are seeded once, not asserted every converge.

Steps 6 and 7 of wordpress_plugins.yml write rows in wp_options that the
WordPress admin UI also writes. They combined current with desired in that
order, and Ansible's `combine` lets the RIGHT-hand side win, so catena's value
won every time: an admin who retuned the NPP preload method, or changed the
sender address under WP Mail SMTP (which this also sets from_email_force on),
had it put back on the next converge with nothing said.

The inversion is the fix, and the marker is what makes the seed unambiguous.
Both are pinned here because both are one word away from silently regressing:
swapping the combine operands back, or dropping the `when` from the marker
write, restores the old behaviour and no other test would notice.

Run: uv run pytest tests/unit/test_wordpress_options_seed_once.py
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

_TASKS = (
    Path(__file__).resolve().parents[3]
    / "ansible" / "reconcile" / "roles" / "infrastructure" / "tasks" / "wordpress_plugins.yml"
)

MARKER_PREFIX = "/var/lib/catena/wordpress-options-bootstrapped-"


def _tasks() -> list[dict]:
    """Every task in the file, flattened out of its blocks."""
    out: list[dict] = []

    def walk(items):
        for t in items or []:
            out.append(t)
            for key in ("block", "always", "rescue"):
                if key in t:
                    walk(t[key])

    walk(yaml.safe_load(_TASKS.read_text()))
    return out


def _by_name(fragment: str) -> dict:
    for t in _tasks():
        if fragment in (t.get("name") or ""):
            return t
    raise AssertionError(f"no task whose name contains {fragment!r}")


def _module(task: dict, name: str):
    """The task's arguments for `name`, whether it is written short or FQCN."""
    for key in (name, f"ansible.builtin.{name}"):
        if key in task:
            return task[key]
    raise AssertionError(f"task {task.get('name')!r} does not use {name}")


def _combine_expr(task: dict) -> str:
    return " ".join(str(next(iter(_module(task, "set_fact").values()))).split())


def test_npp_settings_seed_under_what_the_site_already_has():
    expr = _combine_expr(_by_name("seed NPP settings under whatever"))
    assert re.search(r"_wp_npp_desired\s*\|\s*combine\(", expr), (
        "desired must be the LEFT operand: combine lets the right-hand side "
        f"win, so this order is what keeps the site's own value. Got: {expr}"
    )
    assert "_parsed" in expr


def test_wp_mail_smtp_seeds_under_what_the_site_already_has():
    expr = _combine_expr(_by_name("seed wp_mail_smtp under whatever"))
    assert re.search(r"_wp_mail_smtp_desired\s*\|\s*combine\(", expr), (
        f"desired must be the LEFT operand here too. Got: {expr}"
    )
    assert "recursive=True" in expr, (
        "wp_mail_smtp is nested (mail.*, smtp.*); a shallow combine would "
        "replace a whole sub-map instead of filling its unset keys"
    )


def test_every_option_write_is_gated_on_the_seed_decision():
    """The gate is a single computed fact so the four option tasks cannot drift
    apart. An option write still keyed on _wp_installed alone runs on every
    converge, which is the bug."""
    for fragment in ("declare NPP desired settings",
                     "get current NPP settings",
                     "seed NPP settings under whatever",
                     "update NPP settings",
                     "declare wp-mail-smtp desired option",
                     "get current wp_mail_smtp option",
                     "seed wp_mail_smtp under whatever",
                     "update wp_mail_smtp option"):
        conditions = _by_name(fragment).get("when")
        conditions = [conditions] if isinstance(conditions, str) else conditions
        joined = " ".join(str(c) for c in conditions or [])
        assert "_wp_seed_options" in joined, (
            f"{fragment!r} is not gated on the seed decision: {joined!r}"
        )


def test_the_marker_is_keyed_on_the_stack_not_the_container():
    """A container name carries the swarm task id and changes on every
    reschedule. A marker keyed on that resets itself and re-seeds catena's
    values over the admin's edits -- the exact failure it exists to stop."""
    expr = _combine_expr(_by_name("resolve this site's option-seed marker"))
    assert MARKER_PREFIX in expr
    assert "_wp_stack_name" in expr, (
        "the marker must key on com.docker.stack.namespace, resolved by the "
        "task above it"
    )
    resolver = _by_name("resolve this site's stack name")
    assert "com.docker.stack.namespace" in str(_module(resolver, "command")["argv"])
    assert resolver.get("changed_when") is False


def test_the_marker_is_written_only_on_a_run_that_seeded():
    """Unconditional, it would mark a converge that skipped the writes and no
    later converge would ever seed. Gated, an interrupted run is retried."""
    task = _by_name("mark this site's options as seeded")
    args = _module(task, "file")
    assert args["state"] == "touch"
    assert MARKER_PREFIX in str(args["path"]) or \
        "_wp_seed_marker" in str(args["path"])
    conditions = task.get("when")
    conditions = [conditions] if isinstance(conditions, str) else conditions
    assert "_wp_seed_options" in " ".join(str(c) for c in conditions or [])


def test_a_restored_site_is_treated_as_already_seeded():
    """The marker lives under /var/lib/catena, which is not in backup_paths,
    while the WordPress database that carries the admin's tuned values IS
    restored. So a restored host has the settings and none of the markers, and
    without a pre-drop the seed gate opens and puts catena's NPP preload method
    and WP Mail SMTP sender back over the restored ones -- the same silent
    overwrite the marker exists to stop, reached through DR instead of through
    a converge. reconcile/roles/keycloak drops its three realm markers for this reason;
    this one cannot ride that loop because its name is per site."""
    tasks = _tasks()
    names = [t.get("name") or "" for t in tasks]

    probe = _by_name("probe post-restore marker")
    assert "post-restore.needed" in str(_module(probe, "stat")["path"])

    drop = _by_name("treat a restored site as already seeded")
    args = _module(drop, "file")
    assert args["state"] == "touch"
    assert "_wp_seed_marker" in str(args["path"])
    conditions = drop.get("when")
    conditions = [conditions] if isinstance(conditions, str) else conditions
    assert "_wp_post_restore_marker" in " ".join(str(c) for c in conditions or [])

    def index_of(fragment: str) -> int:
        return next(i for i, n in enumerate(names) if fragment in n)

    assert (index_of("resolve this site's option-seed marker")
            < index_of("treat a restored site as already seeded")
            < index_of("probe the option-seed marker")), (
        "the drop has to land after the marker name is computed and before the "
        "probe that reads it, or the gate is decided on the pre-drop state"
    )


def test_plugin_activation_still_runs_on_every_converge():
    """The seed marker governs OPTIONS only. Install and activate flow forward
    and never deactivate, so they stay unconditional -- a new curated plugin
    must still land on an already-seeded site."""
    for fragment in ("install + activate any missing plugin",
                     "activate any installed-but-inactive plugin"):
        conditions = _by_name(fragment).get("when")
        conditions = [conditions] if isinstance(conditions, str) else conditions
        joined = " ".join(str(c) for c in conditions or [])
        assert "_wp_seed_options" not in joined, (
            f"{fragment!r} was caught by the option-seed gate; a site seeded "
            "once would never receive a newly curated plugin again"
        )
