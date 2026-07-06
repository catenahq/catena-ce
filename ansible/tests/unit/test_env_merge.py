"""Unit tests for the env_merge filter plugin.

Covers the reconcile-not-overwrite merge that keeps operator/client env
edits alive across converges, for BOTH the Dokploy newline-string shape
(merge_env) and the Portainer structured-array shape (merge_env_portainer).

Run: uv run pytest tests/unit/test_env_merge.py
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_PLUGIN = (
    Path(__file__).resolve().parents[3]
    / "ansible" / "playbooks" / "filter_plugins" / "env_merge.py"
)
_spec = importlib.util.spec_from_file_location("env_merge", _PLUGIN)
em = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(em)


# --- merge_env (Dokploy newline-string shape) -------------------------------
def test_merge_env_catalog_key_wins_for_non_credential():
    got = em.merge_env("URL=old", ["URL=new"])
    assert got == ["URL=new"]


def test_merge_env_preserves_unknown_operator_key():
    got = em.merge_env("CUSTOM=typed-by-operator", ["URL=new"])
    assert "CUSTOM=typed-by-operator" in got
    assert "URL=new" in got


def test_merge_env_credential_key_preserved_when_existing_nonempty():
    # Client rotated SMTP_PASSWORD in the UI -> survives the converge.
    got = em.merge_env("SMTP_PASSWORD=client-rotated", ["SMTP_PASSWORD=catalog-seed"])
    assert got == ["SMTP_PASSWORD=client-rotated"]


def test_merge_env_credential_key_seeded_when_existing_blank():
    got = em.merge_env("SMTP_PASSWORD=", ["SMTP_PASSWORD=catalog-seed"])
    assert got == ["SMTP_PASSWORD=catalog-seed"]


def test_merge_env_accepts_dict_desired():
    got = em.merge_env("", [{"name": "K", "value": "v"}])
    assert got == ["K=v"]


# --- merge_env_portainer (structured [{name,value}] shape) ------------------
def test_portainer_first_deploy_normalises_desired():
    # No existing env -> desired projected into Portainer's dict-list shape.
    got = em.merge_env_portainer([], ["URL=https://x", {"name": "PORT", "value": "80"}])
    assert {"name": "URL", "value": "https://x"} in got
    assert {"name": "PORT", "value": "80"} in got


def test_portainer_catalog_key_wins_for_non_credential():
    existing = [{"name": "URL", "value": "old"}]
    got = em.merge_env_portainer(existing, ["URL=new"])
    assert got == [{"name": "URL", "value": "new"}]


def test_portainer_preserves_unknown_operator_key():
    existing = [{"name": "CUSTOM", "value": "typed"}]
    got = em.merge_env_portainer(existing, ["URL=new"])
    assert {"name": "CUSTOM", "value": "typed"} in got
    assert {"name": "URL", "value": "new"} in got


def test_portainer_credential_key_preserved_when_existing_nonempty():
    existing = [{"name": "APP_SECRET", "value": "client-rotated"}]
    got = em.merge_env_portainer(existing, ["APP_SECRET=catalog-seed"])
    assert got == [{"name": "APP_SECRET", "value": "client-rotated"}]


def test_portainer_credential_key_seeded_when_existing_blank():
    existing = [{"name": "APP_SECRET", "value": ""}]
    got = em.merge_env_portainer(existing, ["APP_SECRET=catalog-seed"])
    assert got == [{"name": "APP_SECRET", "value": "catalog-seed"}]


def test_portainer_empty_desired_keeps_existing():
    existing = [{"name": "URL", "value": "keep"}]
    got = em.merge_env_portainer(existing, [])
    assert got == [{"name": "URL", "value": "keep"}]
