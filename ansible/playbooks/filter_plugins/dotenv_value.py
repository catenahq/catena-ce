"""Ansible filter: read one key from the text of an application's .env file.

catena-apps writes the file (catena-admin payload/engines/apps/env.go): one
`KEY=value` per line, a value bare, single-quoted or double-quoted. The last
definition of a key wins, as in Docker Compose. A missing or empty value gives
`default`.
"""

from __future__ import annotations

import re

_LINE = re.compile(r"^[ \t]*(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_.-]*)[ \t]*=[ \t]*(.*)$")


def _value(raw: str) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] == "'":
        return raw[1:-1]
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        return re.sub(r"\\(.)", r"\1", raw[1:-1])
    return raw.split(" #", 1)[0].strip()


def dotenv_value(text, key, default=""):
    """The value of `key` in the .env `text`, or `default` when the key is
    missing or empty."""
    found = ""
    for line in (text or "").splitlines():
        match = _LINE.match(line)
        if match and match.group(1) == key:
            found = _value(match.group(2))
    return found or default


class FilterModule:
    def filters(self):
        return {"dotenv_value": dotenv_value}
