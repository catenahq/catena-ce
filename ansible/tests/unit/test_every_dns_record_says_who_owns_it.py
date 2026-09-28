"""Every DNS record the converge writes carries the catena ownership marker.

Cleanup -- the bench's zone teardown, and the engine that takes a retired
domain's records down when a client changes their domain -- tells catena's
records from the client's by one thing: a comment starting with "Managed by
catena". A record without it is indistinguishable from one the client added by
hand, and the only safe thing to do with those is leave them, forever, on a
domain nobody serves any more.

Both halves are gated, because they failed differently:

  - A CREATE with no comment. The two mail TXT records were written that way
    from the start.
  - An UPDATE that drops it. A Cloudflare PUT REPLACES the record, so a body
    without the comment silently strips it. The coturn and mailserver A
    records both did this, which means a record only stayed recognisable for
    as long as it never drifted. The tunnel engine's own wildcard write
    carries the same note for the same reason.

Run: uv run pytest tests/unit/test_every_dns_record_says_who_owns_it.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

ANSIBLE_DIR = Path(__file__).resolve().parents[2]
RECONCILE_DIR = ANSIBLE_DIR / "reconcile"
OWNERSHIP_PREFIX = "Managed by catena"

# The Cloudflare endpoint that creates or replaces a record. A read carries no
# body and is not a write; a DELETE removes the record and has nothing to mark.
_RECORDS_PATH = "/dns_records"
_WRITE_METHODS = ("POST", "PUT")


def _every_task(node) -> list[dict]:
    out: list[dict] = []
    if isinstance(node, list):
        for item in node:
            out += _every_task(item)
    elif isinstance(node, dict):
        out.append(node)
        for key in ("block", "rescue", "always"):
            if key in node:
                out += _every_task(node[key])
    return out


def _is_record_write(uri: dict) -> bool:
    """A uri task that creates or replaces a Cloudflare DNS record.

    The method may be a Jinja expression choosing between POST and PUT -- the
    mail TXT records are written by one task that does both -- so the test is
    whether a write verb appears in it at all, not equality.
    """
    url = str(uri.get("url") or "")
    if _RECORDS_PATH not in url:
        return False
    method = str(uri.get("method") or "GET").upper()
    return any(verb in method for verb in _WRITE_METHODS)


def _record_writes() -> list[tuple[str, str, dict]]:
    """(file, task name, uri args) for every DNS-record write in reconcile/."""
    out: list[tuple[str, str, dict]] = []
    for path in sorted(RECONCILE_DIR.rglob("*.yml")):
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as e:
            raise AssertionError(f"{path} does not parse: {e}") from e
        for task in _every_task(loaded):
            uri = task.get("ansible.builtin.uri") or task.get("uri")
            if isinstance(uri, dict) and _is_record_write(uri):
                out.append((
                    str(path.relative_to(ANSIBLE_DIR)),
                    str(task.get("name", "<unnamed>")),
                    uri,
                ))
    return out


def test_there_are_dns_record_writes_to_check() -> None:
    """The gate is worthless if the collector finds nothing -- a refactor that
    moves these calls would make every assertion below vacuously true."""
    assert _record_writes(), (
        "no Cloudflare DNS-record writes found under reconcile/. Either they "
        "moved, or the URL/method shape this gate matches on has changed"
    )


def test_every_dns_record_write_carries_the_ownership_comment() -> None:
    offenders: list[str] = []
    for where, name, uri in _record_writes():
        comment = str((uri.get("body") or {}).get("comment") or "")
        if not comment.startswith(OWNERSHIP_PREFIX):
            offenders.append(f"{where}: {name!r} (comment={comment!r})")
    assert not offenders, (
        f"these DNS writes do not mark the record as catena's, so cleanup "
        f"cannot tell them from a record the client wrote and leaves them on "
        f"a domain the host has stopped serving: {offenders}"
    )
