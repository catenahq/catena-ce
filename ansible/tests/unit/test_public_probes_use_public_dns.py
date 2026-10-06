"""Every check that reaches this server through its public names goes through
helpers/public_https.py, so the machine running it resolves those names the way
the internet does rather than through a resolver that may block the domain.

Run: uv run pytest tests/unit/test_public_probes_use_public_dns.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

ANSIBLE_DIR = Path(__file__).resolve().parents[2]
SCANNED = [*sorted((ANSIBLE_DIR / "reconcile").rglob("*.yml")),
           *sorted((ANSIBLE_DIR / "playbooks").rglob("*.yml"))]


def _tasks(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _tasks(value)
    elif isinstance(node, list):
        for item in node:
            yield from _tasks(item)


def _uri_urls():
    for path in SCANNED:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        for task in _tasks(doc):
            for key in ("uri", "ansible.builtin.uri"):
                args = task.get(key)
                if isinstance(args, dict) and "url" in args:
                    yield path, str(args["url"])


def test_no_uri_task_requests_a_public_hostname():
    """uri resolves through this machine's resolver. The Cloudflare and
    Tailscale APIs are other people's names and stay on it."""
    offenders = [f"{p.relative_to(ANSIBLE_DIR)}: {url}"
                 for p, url in _uri_urls()
                 if url.startswith("https://{{") or "jwks" in url]
    assert not offenders, offenders


def test_the_public_probes_call_the_helper():
    probes = {
        "reconcile/roles/oauth2_proxy/tasks/validate.yml": 2,
        "reconcile/roles/infrastructure/tasks/verify_gated_services.yml": 1,
        "playbooks/validate.yml": 2,
    }
    for rel, want in probes.items():
        doc = yaml.safe_load((ANSIBLE_DIR / rel).read_text(encoding="utf-8"))
        got = sum(1 for t in _tasks(doc)
                  if "public_https.py" in str((t.get("ansible.builtin.command") or {})
                                              .get("argv", "")))
        assert got == want, f"{rel}: {got} probe(s) through public_https.py, want {want}"
