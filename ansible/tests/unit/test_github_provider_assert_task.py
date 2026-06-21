"""Static checks on the GitHub-App preflight task. We don't run
Ansible end-to-end here -- bench scenarios cover that. This suite
guards against the easy mistakes:

  - the file parses as YAML,
  - the four expected steps are present and in order,
  - the Dokploy API endpoints used match what we documented in
    internal_docs/operator/github-app-setup.md (so the docs and
    the code do not drift),
  - the assertion fail_msg includes the operator walkthrough
    pointer (so a converge that fails mid-flight gives the
    operator enough to fix without asking).
"""

from __future__ import annotations

from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[3]
TASK_FILE = (
    REPO_ROOT
    / "ansible"
    / "roles"
    / "infrastructure"
    / "tasks"
    / "_github_provider_assert.yml"
)


def _load() -> list[dict]:
    return yaml.safe_load(TASK_FILE.read_text())


def test_task_file_parses() -> None:
    tasks = _load()
    assert isinstance(tasks, list), "task file must be a YAML list"
    assert len(tasks) >= 4, (
        "expected at least 4 tasks: list providers, build map, "
        "assert each alias, probe each alias"
    )


def test_task_endpoints_match_docs() -> None:
    tasks = _load()
    uri_urls = [
        t["ansible.builtin.uri"]["url"]
        for t in tasks
        if "ansible.builtin.uri" in t
    ]
    # Two HTTP calls: list providers (GET) + per-alias testConnection (POST).
    assert any(
        url.endswith("/github.githubProviders") for url in uri_urls
    ), "task must call /github.githubProviders to enumerate providers"
    assert any(
        url.endswith("/github.testConnection") for url in uri_urls
    ), "task must call /github.testConnection per alias"


def test_assert_step_carries_operator_walkthrough() -> None:
    tasks = _load()
    asserts = [t for t in tasks if "ansible.builtin.assert" in t]
    assert asserts, "task must include an ansible.builtin.assert step"
    assert_step = asserts[0]["ansible.builtin.assert"]
    msg = assert_step["fail_msg"]
    # The message must guide the operator to the right Dokploy UI
    # path and the screenshot walkthrough doc, otherwise a fresh
    # operator hits a wall.
    assert "/dashboard/settings/git-providers" in msg
    assert "github-app-setup.md" in msg


def test_loop_var_is_alias() -> None:
    """The loops iterate over `expected_github_providers` and bind
    the current entry to `alias`. Test fixture for the bench
    scenarios depends on this name."""
    tasks = _load()
    looped = [t for t in tasks if "loop" in t]
    assert looped, "expected at least one looped task"
    for t in looped:
        assert t["loop"] == "{{ expected_github_providers }}"
        assert t["loop_control"]["loop_var"] == "alias"


def test_uri_calls_delegate_to_localhost() -> None:
    """Every URI call must run from the operator laptop (the only
    place dokploy_api_base is reachable), matching the convention
    in _dokploy_template_drift_heal.yml."""
    tasks = _load()
    for t in tasks:
        if "ansible.builtin.uri" in t:
            assert t.get("delegate_to") == "localhost", (
                f"URI step missing delegate_to: localhost: "
                f"{t.get('name', '<unnamed>')}"
            )
