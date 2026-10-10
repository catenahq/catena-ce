"""Lock the migration lane's CE-side wiring.

The lane itself is a host process in the private repo. What lives here is
everything that decides how it is reachable and how it is armed, and each
of those has a way of going quietly wrong:

  1. A server taking this one over reaches the lane through sshd alone: the
     migration account, its Match block and its place in AllowUsers. The
     account takes only the key of an open migration window, read from the
     one file the lane writes while the window is open, and runs only the
     pipe to the lane's socket. Those rules are bootstrap-owned, so only an
     install applies them, and no port is declared for the lane.
  2. The eight action names are authorised by the payload's own drop-ins
     (the source side's four in catena-admin payload/actions.d/10-paid.sh,
     the receiving side's four in 20-catena.sh), not by either of
     this repo's dispatch lists. Every command behind them is a binary
     the payload installs at a path the payload chose, so an entry here
     would authorise a name this repo neither owns nor can verify. What
     each command must LOOK like is asserted there; what is asserted here
     is that this repo carries none of them, because a name in both places
     is dispatched by the converge's arm and the drop-in is never reached.
  3. The lane's state files stay separate from the restore machine's. A
     migration DRIVES a restore, so one shared file would have the page
     watching the move read the restore's progress as its own.

Run: uv run pytest tests/unit/test_catena_admin_migrate_lane.py
"""
from __future__ import annotations

from pathlib import Path

import jinja2
import yaml

_ANSIBLE = Path(__file__).resolve().parents[2]
_ROLE = _ANSIBLE / "reconcile" / "roles" / "catena-admin"
DEFAULTS = _ROLE / "defaults" / "main.yml"
LANES_TASKS = _ROLE / "tasks" / "lanes.yml"
# The bootstrap-side half of the same panel: the runner account, the
# sudoers drop-in, the forced command, and the migration account. A role of
# its own, so the bootstrap/reconcile boundary it sits on is one the layout
# can hold.
_HOST_ROLE = _ANSIBLE / "bootstrap" / "roles" / "catena_admin_host"
_HOST_DEFAULTS = _HOST_ROLE / "defaults" / "main.yml"
_HOST_TASKS = _HOST_ROLE / "tasks" / "main.yml"
# And the values both halves read, which belong to neither role.
_GROUP_VARS = _ANSIBLE / "playbooks" / "group_vars" / "all" / "main.yml"
_COMMON = _ANSIBLE / "bootstrap" / "roles" / "common"
_BACKUP_DEFAULTS = _ANSIBLE / "reconcile" / "roles" / "backup" / "defaults" / "main.yml"

MIGRATE_DROP_IN = "/etc/ssh/sshd_config.d/31-catena-migrate.conf"

MIGRATE_ACTIONS = {
    "catena-migrate-arm",
    "catena-migrate-disarm",
    "catena-migrate-status",
    "catena-migrate-resume",
    # Target side: this server pulling another one onto itself.
    "catena-migrate-run",
    "catena-migrate-run-status",
    "catena-migrate-run-reset",
    "catena-migrate-resume-source",
}


def _defaults() -> dict:
    """Every variable the panel's converge reads, from all three places it
    lives.

    The trust path is bootstrap/roles/catena_admin_host, the container and the
    lanes' configuration are reconcile/roles/catena-admin, and the values BOTH
    halves need are in group_vars because a role default is only dependable
    once that role has run. Merged here so an assertion is about the panel's
    configuration rather than about which file holds a line.
    """
    merged: dict = {}
    for path in (_GROUP_VARS, DEFAULTS, _HOST_DEFAULTS):
        merged.update(yaml.safe_load(path.read_text()) or {})
    return merged


def _ee_actions() -> dict[str, str]:
    return {a["name"]: a["shell"] for a in _defaults()["catena_admin_ee_reserved_actions"]}


def _host_tasks() -> list[dict]:
    return yaml.safe_load(_HOST_TASKS.read_text()) or []


def _migrate_account_task() -> dict:
    tasks = [t for t in _host_tasks()
             if (t.get("ansible.builtin.user") or {}).get("name")
             == "{{ catena_migrate_user }}"]
    assert len(tasks) == 1, tasks
    return tasks[0]


def _drop_in_task() -> tuple[int, dict]:
    tasks = _host_tasks()
    found = [(i, t) for i, t in enumerate(tasks)
             if (t.get("ansible.builtin.copy") or {}).get("dest") == MIGRATE_DROP_IN]
    assert len(found) == 1, found
    return found[0]


def _render(text: str) -> str:
    d = _defaults()
    return jinja2.Environment(undefined=jinja2.StrictUndefined).from_string(text).render(
        ansible_managed="managed",
        catena_migrate_user=d["catena_migrate_user"],
        catena_migrate_grant_file=d["catena_migrate_grant_file"],
        catena_migrate_connect=d["catena_migrate_connect"],
    )


def _match_blocks(conf: str) -> list[tuple[str, dict[str, str]]]:
    """(Match line, {keyword: value}) per block, in file order."""
    blocks: list[tuple[str, dict[str, str]]] = []
    for raw in conf.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        keyword, _, value = line.partition(" ")
        if keyword == "Match":
            blocks.append((line, {}))
            continue
        assert blocks, f"{line!r} sits outside a Match block"
        assert keyword not in blocks[-1][1], f"{keyword} given twice"
        blocks[-1][1][keyword] = value.strip()
    return blocks


def test_the_migration_actions_left_this_repo():
    """They live in the payload drop-in. A name here would be dispatched by
    the converge's own case arm, and the drop-in -- which is where the command
    is maintained -- would never be reached.

    The lane's state files and its binary's install path are the payload's
    too: they are product constants, and a constant is owned by whoever ships
    the thing it points at. That the two state files stay distinct from the restore machine's is
    asserted in catena-admin, where both pairs are declared."""
    assert not MIGRATE_ACTIONS & set(_ee_actions())
    for gone in ("catena_migrate_lane_bin", "catena_migration_state_file",
                 "catena_migration_history_file"):
        assert gone not in _defaults(), (
            f"{gone} is back in this repo; the binary and its state files "
            "belong to the payload that installs them"
        )


def test_the_migration_account_is_key_only_with_no_password():
    """`*` is no valid password without reading as locked, which `!` does and
    sshd then refuses key auth for. The shell has to run a command, because
    sshd runs ForceCommand through it. The home is root's, so the account owns
    nothing it could write a key or an rc file into, and it holds nothing a
    backup should carry."""
    user = _migrate_account_task()["ansible.builtin.user"]
    assert user["password"] == "*"
    assert user["system"] is True
    assert user["shell"] == "/bin/sh"
    assert user["home"] == "{{ catena_migrate_home }}"
    assert user.get("create_home") is False
    assert "groups" not in user

    home = _defaults()["catena_migrate_home"]
    dirs = [t["ansible.builtin.file"] for t in _host_tasks()
            if (t.get("ansible.builtin.file") or {}).get("path")
            == "{{ catena_migrate_home }}"]
    assert dirs == [{"path": "{{ catena_migrate_home }}", "state": "directory",
                     "owner": "root", "group": "root", "mode": "0755"}]

    backed_up = yaml.safe_load(_BACKUP_DEFAULTS.read_text())["backup_product_paths"]
    inside = [p for p in backed_up if home == p or home.startswith(p.rstrip("/") + "/")]
    assert not inside, f"{home} is inside the backup set: {inside}"


def test_the_migration_match_block_runs_only_the_lane_pipe():
    """Exactly these rules, so a keyword that would open more than the pipe
    cannot be added without this test changing. The grant file is absolute,
    so sshd never reads a file under the home, and the block closes with
    `Match all` so nothing a later drop-in sets lands inside it."""
    d = _defaults()
    assert d["catena_migrate_user"] == "catena-migrate"
    assert d["catena_migrate_grant_file"] == "/run/catena-migrate-lane/authorized_keys"
    assert d["catena_migrate_connect"] == "/usr/local/bin/catena-migrate-lane connect"
    assert d["catena_migrate_grant_file"].startswith("/")

    _, task = _drop_in_task()
    copy = task["ansible.builtin.copy"]
    assert (copy["owner"], copy["group"], copy["mode"]) == ("root", "root", "0644")
    assert task["notify"] == "reload sshd"

    blocks = _match_blocks(_render(copy["content"]))
    assert [line for line, _ in blocks] == [
        f"Match User {d['catena_migrate_user']}", "Match all"]
    assert blocks[0][1] == {
        "AuthorizedKeysFile": d["catena_migrate_grant_file"],
        "AuthorizedKeysCommand": "none",
        "ForceCommand": d["catena_migrate_connect"],
        "PermitTTY": "no",
        "AllowTcpForwarding": "no",
        "AllowStreamLocalForwarding": "no",
        "AllowAgentForwarding": "no",
        "X11Forwarding": "no",
        "PermitTunnel": "no",
        "PermitUserRC": "no",
        "ClientAliveInterval": "30",
        "ClientAliveCountMax": "4",
    }
    assert blocks[1][1] == {}


def test_the_sshd_config_is_checked_after_the_drop_in():
    """The drop-in lands before the reload, so a rule sshd refuses has to fail
    the run there, while the config sshd last loaded still serves."""
    tasks = _host_tasks()
    i, _ = _drop_in_task()
    check = tasks[i + 1]
    assert check["ansible.builtin.command"]["argv"] == ["/usr/sbin/sshd", "-t"]
    assert "failed_when" not in check
    assert check["changed_when"] is False


def test_allow_users_names_the_migration_account():
    """AllowUsers is one line sshd takes the first value of, so an account
    absent from it is refused whatever its Match block says."""
    users = yaml.safe_load((_COMMON / "defaults" / "main.yml").read_text())["ssh_allowed_users"]
    assert "{{ catena_migrate_user }}" in users

    validate = yaml.safe_load((_COMMON / "tasks" / "validate.yml").read_text())
    check = next(t for t in validate
                 if t.get("name") == "validate/common: sshd drop-in has hardening directives")
    assert any("catena_migrate_user" in c and "^AllowUsers" in c
               for c in check["ansible.builtin.assert"]["that"])


def test_the_migration_account_is_created_by_bootstrap_alone():
    """The way in is the install's to set, as the runner account is: a
    reconcile able to write it could open the host to whoever it chose."""
    boundary = yaml.safe_load((_ANSIBLE / "boundary.yml").read_text())
    assert "catena_admin_host" in {r["name"] for r in boundary["bootstrap_roles"]}

    plays = yaml.safe_load((_ANSIBLE / "playbooks" / "reconcile.yml").read_text())
    roles = [r["role"] if isinstance(r, dict) else r
             for play in plays for r in (play.get("roles") or [])]
    assert "catena_admin_host" not in roles

    writers = []
    for path in sorted((_ANSIBLE / "reconcile" / "roles").rglob("tasks/*.yml")):
        text = path.read_text(encoding="utf-8")
        if "catena_migrate_user" in text or "31-catena-migrate" in text:
            writers.append(str(path.relative_to(_ANSIBLE)))
    assert not writers, writers


def test_no_port_is_declared_for_the_migration_lane():
    """The lane listens on a unix socket, and a server taking this one over
    reaches it through sshd on 22, which bootstrap/roles/common declares. A
    lane port in the registry would be a firewall rule for a listener that is
    never there."""
    assert "catena_migrate_lane_port" not in _defaults()
    tasks = yaml.safe_load(LANES_TASKS.read_text())
    fragment = next(t["ansible.builtin.copy"] for t in tasks
                    if str((t.get("ansible.builtin.copy") or {}).get("dest", ""))
                    .endswith("/catena-admin.json"))
    content = fragment["content"]
    assert content.count('"port":') == 1
    assert '"port": catena_admin_ui_port | int' in content
    assert '"scope": "tailnet"' not in content
