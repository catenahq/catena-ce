"""No text in the tree places a role by its number in a playbook.

converge.yml and reconcile.yml list different roles, and the lists change, so
"role 5.5" or "converge.yml position 9" is wrong in one playbook from the day
it is written and in both soon after.
The order is said relative to the roles it is about ("before
reconcile/roles/catena-admin in both converge playbooks"), which stays true
while that order does.

Run: uv run pytest tests/unit/test_no_role_numbers_in_comments.py
"""
from __future__ import annotations

import re
from pathlib import Path

ANSIBLE = Path(__file__).resolve().parents[2]

# Lowercase `role`: Portainer's "Role 2" is a role id, not a position.
_BANNED = re.compile(
    r"\brole (?:position )?\d+(?:\.\d+)?\b"
    r"|\bposition \d+\b"
    r"|\bruns at \d+(?:\.\d+)?\b"
)
_SUFFIXES = {".yml", ".yaml", ".j2", ".py", ".sh", ".md", ".cfg"}
_SKIP = ("/.collections/", "/.venv/", "/.ansible/", "/inventory/")


def test_no_role_is_placed_by_number():
    offenders = []
    for path in sorted(ANSIBLE.rglob("*")):
        rel = "/" + str(path.relative_to(ANSIBLE))
        if path.suffix not in _SUFFIXES or any(s in rel for s in _SKIP):
            continue
        if path.name == Path(__file__).name or not path.is_file():
            continue
        for n, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
            if _BANNED.search(line):
                offenders.append(f"{rel[1:]}:{n}: {line.strip()}")
    assert not offenders, (
        "say where a role runs relative to the roles it is about, not by its "
        "number in a playbook:\n" + "\n".join(offenders))
