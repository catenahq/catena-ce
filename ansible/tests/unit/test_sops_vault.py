"""Unit test for the Community installer's SOPS encrypt cwd contract.

sops discovers .sops.yaml by walking UP from its working directory (not
from --filename-override, which only selects the matching creation_rule).
seed.py writes inventory/<inv>/.sops.yaml with the self recipient, so
encrypt_text must run sops with cwd = the target file's own directory.
Otherwise `catena install` invoked from the repo root (the test bench
does exactly this) walks up from there, never reaches the per-inventory
.sops.yaml, and sops fails "config file not found, or has no creation
rules".
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ANSIBLE_DIR = Path(__file__).resolve().parents[2]
if str(ANSIBLE_DIR) not in sys.path:
    sys.path.insert(0, str(ANSIBLE_DIR))

from helpers import sops_vault  # noqa: E402


def test_encrypt_text_runs_sops_in_target_dir(tmp_path, monkeypatch):
    target = (
        tmp_path / "inventory" / "test" / "group_vars" / "all" / "vault.sops.yml"
    )
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["cwd"] = kwargs.get("cwd")
        return subprocess.CompletedProcess(cmd, 0, stdout="ENC\n", stderr="")

    monkeypatch.setattr(sops_vault, "_ensure_age_key", lambda env: dict(env or {}))
    monkeypatch.setattr(sops_vault.subprocess, "run", fake_run)

    sops_vault.encrypt_text("foo: bar\n", target, env={"SOPS_AGE_KEY": "stub"})

    # The fix: sops runs in the target's directory so .sops.yaml discovery
    # (cwd walk-up) reaches inventory/test/.sops.yaml.
    assert captured["cwd"] == str(target.parent)
    assert captured["cmd"][:2] == ["sops", "-e"]
    assert "--filename-override" in captured["cmd"]
    assert target.read_text() == "ENC\n"
