"""The Portainer API-key mint reads stdin and persists to the on-box store.

Regression: the mint used to require `<inventory>/group_vars/all/vault.yml`
as BOTH the source of vault_admin_password and the destination of the minted
key. `catena install` stops writing that file (0b, seed.py: "No vault.yml:
secrets never persist on the laptop"), so on any real install the helper
returned EXIT_ERROR ("vault not found") and the un-guarded command task
failed the converge. It looked fine only because both bench inventories carry
a hand-maintained vault.yml -- the bench supplying a file the product does
not, which is the substitution trap `audit --check-env-owners` exists to
catch.

Run: uv run pytest tests/unit/test_portainer_apikey_mint.py
"""
from __future__ import annotations

import importlib.util
import io
from pathlib import Path

import pytest
import yaml

ANSIBLE_DIR = Path(__file__).resolve().parents[2]
HELPER = ANSIBLE_DIR / "helpers" / "bootstrap_portainer_admin.py"
ROLE_TASKS = ANSIBLE_DIR / "roles" / "portainer" / "tasks" / "main.yml"
LOADER = ANSIBLE_DIR / "playbooks" / "tasks" / "load_onbox_config.yml"


@pytest.fixture(scope="module")
def bpa():
    spec = importlib.util.spec_from_file_location("bootstrap_portainer_admin", HELPER)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _tasks() -> list[dict]:
    return yaml.safe_load(ROLE_TASKS.read_text()) or []


def _mint_block() -> list[dict]:
    for task in _tasks():
        if "block" in task and "mint Portainer API key" in task.get("name", ""):
            return task["block"]
    raise AssertionError("the mint block is gone")


def _named(block: list[dict], fragment: str) -> dict:
    for task in block:
        if fragment in task.get("name", ""):
            return task
    raise AssertionError(f"no task named ...{fragment}... in {[t.get('name') for t in block]}")


# --- the helper -------------------------------------------------------------
def test_the_password_comes_from_stdin(bpa, monkeypatch, capsys):
    seen: dict = {}

    def _fake_bootstrap(base_url, user, password, **kw):
        seen.update(base_url=base_url, user=user, password=password)
        return "MINTED-KEY"

    monkeypatch.setattr(bpa, "bootstrap", _fake_bootstrap)
    monkeypatch.setattr("sys.stdin", io.StringIO("hunter2\n"))
    rc = bpa.main(["--tailnet-ip", "100.1.2.3", "--port", "9000"])
    assert rc == bpa.EXIT_OK
    assert seen["password"] == "hunter2"
    assert seen["base_url"] == "http://100.1.2.3:9000"
    # stdout carries ONLY the key -- the role registers it into a set_fact.
    assert capsys.readouterr().out.strip() == "MINTED-KEY"


def test_a_trailing_space_in_the_password_survives(bpa, monkeypatch):
    """.strip() here would mint against a different password than the one
    Portainer's admin was created with, and the failure would surface as a
    401 nobody could explain."""
    seen: dict = {}
    monkeypatch.setattr(bpa, "bootstrap",
                        lambda *a, **k: seen.update(password=a[2]) or "K")
    monkeypatch.setattr("sys.stdin", io.StringIO("pw with space \n"))
    assert bpa.main(["--tailnet-ip", "1.2.3.4"]) == bpa.EXIT_OK
    assert seen["password"] == "pw with space "


def test_empty_stdin_is_an_error_not_an_anonymous_signin(bpa, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert bpa.main(["--tailnet-ip", "1.2.3.4"]) == bpa.EXIT_ERROR


def test_the_helper_takes_no_vault_argument(bpa, monkeypatch):
    """A --vault flag is the regression itself: it would mean a file on the
    laptop is load-bearing again."""
    monkeypatch.setattr("sys.stdin", io.StringIO("pw"))
    with pytest.raises(SystemExit):
        bpa.main(["--tailnet-ip", "1.2.3.4", "--vault", "/tmp/x.yml"])


def test_the_helper_does_not_import_yaml():
    assert "import yaml" not in HELPER.read_text()


# --- the role wiring --------------------------------------------------------
def test_the_mint_passes_the_password_on_stdin_not_argv():
    task = _named(_mint_block(), "invoke bootstrap_portainer_admin.py")
    cmd = task["ansible.builtin.command"]
    assert cmd["stdin"] == "{{ vault_admin_password }}"
    assert cmd["stdin_add_newline"] is False
    # argv is world-readable through /proc while the process lives.
    assert not any("vault_admin_password" in str(a) for a in cmd["argv"])
    assert not any("--vault" in str(a) for a in cmd["argv"])
    assert task["no_log"] is True


def test_the_minted_key_is_published_and_persisted_on_box():
    block = _mint_block()
    assert _named(block, "publish the minted key")["ansible.builtin.set_fact"] == {
        "vault_portainer_api_key": "{{ _pt_apikey_mint.stdout | trim }}"
    }
    write = _named(block, "write the minted key into the on-box config store")
    cmd = write["ansible.builtin.script"]["cmd"]
    assert "onbox_config.py" in cmd
    # --overwrite: the 401/403 arm means the STORED key is the dead one.
    assert "--overwrite" in cmd
    assert "--adopt-file /root/.catena-portainer-key.json" in cmd


def test_the_staged_key_file_is_0600_and_removed():
    block = _mint_block()
    stage = _named(block, "stage the minted key")["ansible.builtin.copy"]
    assert stage["mode"] == "0600"
    rm = _named(block, "remove the staged key file")["ansible.builtin.file"]
    assert rm["path"] == stage["dest"] and rm["state"] == "absent"


def test_no_task_in_the_role_reads_or_writes_an_inventory_vault():
    assert "group_vars/all/vault.yml" not in ROLE_TASKS.read_text()


def test_the_loader_publishes_the_stored_key():
    """Excluding it made the role re-mint on EVERY converge once the laptop
    vault stopped supplying it as an inventory var."""
    body = LOADER.read_text()
    assert "catena_onbox_fact_exclude" not in body
    assert "rejectattr" not in body


def test_the_mint_decision_is_taken_before_the_block():
    """A block's `when` is re-evaluated for EVERY task in it.

    The block set_facts vault_portainer_api_key as its second task -- the
    same variable an inline condition would read. Inline, the mint ran, the
    key was published as a fact, and then every REMAINING task in the block
    skipped because the condition had just flipped false. The key never
    reached the on-box store, every later Portainer-API step logged "no API
    key yet", and the converge reported success having deployed no gated app.

    So the decision has to be a fact taken once, before the block, immune to
    what the block does to the variable it was derived from.
    """
    tasks = _tasks()
    names = [t.get("name", "") for t in tasks]
    decide = next(i for i, n in enumerate(names)
                  if "decide whether an API key must be minted" in n)
    block = next(i for i, n in enumerate(names) if "mint Portainer API key" in n)
    assert decide < block, "the decision must be taken before the block"

    # The block gates on the captured fact, NOT on the mutated variable.
    guard = str(tasks[block].get("when", ""))
    assert "_pt_apikey_needs_mint" in guard
    assert "vault_portainer_api_key" not in guard, (
        "the block reads the variable its own set_fact overwrites; every task "
        "after that set_fact will skip"
    )


def test_the_minted_key_is_persisted_to_the_on_box_store():
    """Minting without persisting leaves a host that works for the rest of
    one converge and has no key on the next."""
    block = next(t for t in _tasks() if "mint Portainer API key" in t.get("name", ""))
    inner = [t.get("name", "") for t in block.get("block", [])]
    assert any("write the minted key into the on-box config store" in n for n in inner)
    assert any("stage the minted key for the on-box store" in n for n in inner)
