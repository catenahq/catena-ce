"""
Custom Ansible lookup plugin: read values from a .env file alongside the active
inventory (so multiple clients / environments can coexist in one repo).

Usage in group_vars / playbooks:
    "{{ lookup('dotenv', 'SSH_PRIVATE_KEY') }}"
    "{{ lookup('dotenv', 'TAILSCALE_ACCEPT_DNS', default='false') }}"

Search order:
  1. <inventory_dir>/.env       -- per-inventory config (the canonical location)
  2. cwd/.env                   -- legacy fallback for single-inventory usage
  3. playbook_dir/../.env       -- legacy fallback
  4. playbook_dir/.env          -- legacy fallback

Missing keys raise AnsibleError unless `default` is passed -- fail-fast by design;
silent defaults hide misconfiguration.

Why a custom plugin rather than system env vars:
  - No "source .env" step in operator workflow, no wrapper script required.
  - `.env` is the single source of truth -- edit one file, ansible sees it.
  - Secrets never go here (vault.yml handles those); .env is non-secret only.
"""

from __future__ import annotations

import os
from typing import Any

from ansible.errors import AnsibleError
from ansible.plugins.lookup import LookupBase


class LookupModule(LookupBase):
    def run(self, terms: list[str], variables: dict[str, Any] | None = None, **kwargs: Any) -> list[str]:
        dotenv_path = kwargs.get("file") or self._find_dotenv(variables or {})
        env = self._load(dotenv_path) if dotenv_path else {}

        results: list[str] = []
        for key in terms:
            if key in env:
                results.append(env[key])
                continue
            if "default" in kwargs:
                results.append(str(kwargs["default"]))
                continue
            raise AnsibleError(
                f"dotenv: key {key!r} not found in "
                f"{dotenv_path or '(no .env file located)'}. "
                f"Add it to .env, or call the lookup with default=..."
            )
        return results

    def _find_dotenv(self, variables: dict[str, Any]) -> str | None:
        candidates: list[str] = []

        # Primary: per-inventory .env. `inventory_dir` is a built-in Ansible
        # magic var and is populated even during group_vars evaluation.
        inventory_dir = variables.get("inventory_dir")
        if inventory_dir:
            candidates.append(os.path.join(inventory_dir, ".env"))

        # Legacy fallbacks -- preserved so single-inventory installs that
        # haven't migrated their .env yet keep working.
        candidates.append(os.path.join(os.getcwd(), ".env"))

        playbook_dir = variables.get("playbook_dir")
        if playbook_dir:
            candidates.append(os.path.abspath(os.path.join(playbook_dir, "..", ".env")))
            candidates.append(os.path.join(playbook_dir, ".env"))

        for candidate in candidates:
            if os.path.isfile(candidate):
                return os.path.abspath(candidate)
        return None

    def _load(self, path: str) -> dict[str, str]:
        env: dict[str, str] = {}
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                # Strip matching surrounding quotes (single or double).
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                    value = value[1:-1]
                env[key] = value
        return env
