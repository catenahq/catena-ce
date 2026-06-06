"""Ansible filter: merge desired env vars into an existing multi-line env
string, preserving operator/client edits for keys the catalog doesn't own
AND for credential-shaped keys whose existing value is non-empty.

Dokploy stores compose env vars as a single newline-separated string (the
literal text of its in-UI env editor). Every converge that re-POSTs
compose.update with just our catalog env would clobber anything the
operator or client typed in that editor. This filter merges:

  - Keys NOT in desired (operator/client typed them in Dokploy UI) are
    preserved verbatim.
  - Credential-shaped keys (matching CLIENT_ROTATABLE_SUFFIXES) IN
    desired are preserved when the existing value is non-empty -- this
    is the reconcile-not-overwrite rule that lets a client rotate SMTP
    or app passwords via Dokploy's env tab and survive the next
    converge. On first install (existing empty), the catalog value
    wins and seeds the field.
  - Other keys in desired (URLs, hostnames, feature flags) win
    unconditionally; ansible owns those.
  - Blank lines and comment lines (`#...`) in existing are preserved
    in their original position so the editor view stays readable.
  - Keys in desired not present in existing are appended at the end.

The filter is registered as `merge_env` and called like:

    {{ existing_env_string | merge_env(svc_env_list) | join('\n') }}
"""

from __future__ import annotations

# Suffix-based heuristic for "credential-shaped" env keys. If a key
# ends in one of these, the merge prefers an existing non-empty
# value over the catalog value. Non-credential env vars (URLs,
# hostnames, feature flags) are not in this set and are always
# overwritten by catalog values on converge.
CLIENT_ROTATABLE_SUFFIXES = (
    "_PASSWORD",
    "_PASS",
    "_SECRET",
    "_TOKEN",
    "_API_KEY",
    "_ACCESS_KEY",
    "_SECRET_KEY",
)


def _is_client_rotatable(key: str) -> bool:
    return any(key.endswith(suffix) for suffix in CLIENT_ROTATABLE_SUFFIXES)


def _parse_kv(line) -> tuple[str, str] | None:
    # Accept dict shape `{name: K, value: V}` (Dokploy's native env entry
    # form, used by some catena_app callers) AND the legacy "K=V" string
    # shape used by the older callers. Mixing the two within a single
    # svc_env list is harmless.
    if isinstance(line, dict):
        key = str(line.get("name", "")).strip()
        if not key:
            return None
        value = "" if line.get("value") is None else str(line.get("value"))
        return (key, value)
    line = str(line)
    stripped = line.lstrip()
    if not stripped or stripped.startswith("#"):
        return None
    if "=" not in line:
        return None
    key, value = line.split("=", 1)
    key = key.strip()
    if not key:
        return None
    return (key, value)


def merge_env(existing, desired):
    """Merge `desired` (list of 'KEY=value' strings) into `existing` (a
    multi-line string or None), preserving unknown keys, blank lines, and
    comments from `existing`. Returns a list of lines."""

    if existing is None:
        existing_text = ""
    elif isinstance(existing, list):
        existing_text = "\n".join(existing)
    else:
        existing_text = str(existing)

    desired_items: list[tuple[str, str]] = []
    desired_keys: dict[str, str] = {}
    for raw in desired or []:
        kv = _parse_kv(raw)
        if kv is None:
            continue
        desired_items.append(kv)
        desired_keys[kv[0]] = kv[1]

    result: list[str] = []
    seen: set[str] = set()

    for line in existing_text.splitlines():
        kv = _parse_kv(line)
        if kv is None:
            result.append(line)
            continue
        key, existing_value = kv
        seen.add(key)
        if key in desired_keys:
            # Reconcile-not-overwrite: a client-rotatable key with an
            # existing non-empty value survives the converge. Catalog
            # value still seeds the field on first install (existing
            # blank) and still wins for non-credential keys.
            if _is_client_rotatable(key) and existing_value.strip():
                result.append(f"{key}={existing_value}")
            else:
                result.append(f"{key}={desired_keys[key]}")
        else:
            result.append(f"{key}={existing_value}")

    for key, value in desired_items:
        if key not in seen:
            result.append(f"{key}={value}")
            seen.add(key)

    return result


def preserved_env_keys(existing, desired):
    """Return keys present in `existing` that are NOT in `desired` -- i.e.
    the keys that merge_env will preserve as operator/client edits. Used
    for a debug printout after a merge so the operator can see what
    survived a re-converge."""

    if existing is None:
        existing_text = ""
    elif isinstance(existing, list):
        existing_text = "\n".join(existing)
    else:
        existing_text = str(existing)

    desired_keys: set[str] = set()
    for raw in desired or []:
        kv = _parse_kv(raw)
        if kv is not None:
            desired_keys.add(kv[0])

    preserved: list[str] = []
    for line in existing_text.splitlines():
        kv = _parse_kv(line)
        if kv is None:
            continue
        if kv[0] not in desired_keys and kv[0] not in preserved:
            preserved.append(kv[0])
    return preserved


class FilterModule:
    def filters(self):
        return {
            "merge_env": merge_env,
            "preserved_env_keys": preserved_env_keys,
        }
