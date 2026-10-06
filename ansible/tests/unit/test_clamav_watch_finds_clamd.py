"""The clamd watch finds the clamd container by the label the stack sets.

run-clamav-watch.sh looks clamd up with `docker ps --filter label=...`. A
label the clamav stack does not set matches nothing, and the watch then pages
"no clamav container running" every five minutes on every host with a clamd
consumer.

Run: uv run pytest tests/unit/test_clamav_watch_finds_clamd.py
"""
from __future__ import annotations

import re
from pathlib import Path

ANSIBLE = Path(__file__).resolve().parents[2]
WATCH = ANSIBLE / "scripts" / "run-clamav-watch.sh"
COMPOSE = (ANSIBLE / "reconcile" / "roles" / "infrastructure" / "templates"
           / "clamav.compose.yml.j2")


def test_the_watch_asks_for_a_label_the_clamav_stack_sets():
    watch = WATCH.read_text(encoding="utf-8")
    lookup = re.search(r"clamav_ct=\$\(running --filter 'label=([^']+)'\)", watch)
    assert lookup, "run-clamav-watch.sh no longer looks clamd up by one label"
    assert f'- "{lookup.group(1)}"' in COMPOSE.read_text(encoding="utf-8"), (
        f"the clamav stack does not set {lookup.group(1)}, so the watch "
        "finds no clamd container and pages on every run"
    )
