"""Regression guard: gatus-base.yaml.j2 must always define >= 1 endpoint.

Gatus (>= v5.36) panics at boot when the MERGED /config defines zero
endpoints ("configuration should contain at least one endpoint or
suite"). roles/infrastructure/tasks/gatus.yml deploys the container long
before gatus_sync.yml writes the 40-infra.yaml / 50-catena-apps.yaml
endpoint files, so 00-base.yaml (rendered from this template) is the only
config present at first boot. If it carries no endpoints the container
crash-loops -- and the crash-loop churns catena-network hard enough to
starve the converge's own SSH channel, surfacing as a spurious
become-timeout UNREACHABLE on an unrelated task.

This test fails if the self-check endpoint is ever dropped again.

Run: uv run pytest tests/unit/test_gatus_base_selfcheck.py
"""
from __future__ import annotations

from pathlib import Path

_TEMPLATES = (
    Path(__file__).resolve().parents[3]
    / "ansible" / "roles" / "infrastructure" / "templates"
)
_TEMPLATE = _TEMPLATES / "gatus-base.yaml.j2"
_INFRA_SPEC = _TEMPLATES / "gatus-infra-spec.json.j2"


def _uncommented_lines(text: str) -> list[str]:
    # Jinja comment lines and YAML `#` comments both start (after
    # indentation) with `#` or `{#`; drop them so we assert on real config.
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("{#"):
            continue
        out.append(line)
    return out


def test_base_template_defines_at_least_one_endpoint():
    lines = _uncommented_lines(_TEMPLATE.read_text())
    # A real top-level `endpoints:` key (column 0), not a comment mention.
    assert any(l.rstrip() == "endpoints:" for l in lines), (
        "gatus-base.yaml.j2 must declare a top-level `endpoints:` list -- "
        "Gatus v5.36+ panics at boot on an endpoint-less merged config, "
        "and gatus-sync's endpoint files are not written until much later "
        "in the converge."
    )
    # At least one list entry under it.
    assert any(l.strip().startswith("- name:") for l in lines), (
        "gatus-base.yaml.j2 `endpoints:` list is empty -- needs the "
        "self-check probe so the container boots before gatus-sync runs."
    )


def test_selfcheck_probes_own_health_on_the_web_port():
    text = _TEMPLATE.read_text()
    # Probe Gatus's own /health on the templated web port; a status
    # condition makes it a valid, self-contained endpoint from first boot.
    assert "/health" in text
    assert "localhost:{{ gatus_internal_port }}/health" in text
    assert "[STATUS] == 200" in text


def test_the_selfcheck_joins_the_same_group_as_the_infra_endpoints():
    """Gatus groups endpoints by the literal string. The self-check said
    `infrastructure` and every entry in gatus-infra-spec.json.j2 says
    `Infrastructure`, so the status page rendered two sections with the same
    name and one endpoint stranded in the wrong one."""
    groups = {
        line.split(":", 1)[1].strip().strip('",')
        for line in _TEMPLATE.read_text().splitlines() + _INFRA_SPEC.read_text().splitlines()
        if line.strip().startswith(('group:', '"group":'))
    }
    assert groups == {"Infrastructure"}, (
        f"the Gatus endpoint templates spell one group more than one way: {sorted(groups)}"
    )
