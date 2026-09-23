"""A `private` port says never public, and the host says which private path.

TWO DIFFERENT THINGS SHARED ONE WORD. The Portainer and catena-admin UI ports
mean "never reachable from the public internet" and do not care how they are
reached instead. The migration lane means `tailnet` literally: it binds this
host's tailnet address and refuses to start without one.

Declare both as `tailnet` and both come out the same -- and on a host with no
tailnet the defect is honesty rather than enforcement. `tailnet` emits the
RFC1918 rules plus one `tailscale0` interface rule; with no such interface
iptables accepts that rule and it never matches, so the port stays correctly
private. The damage is to the generated document: `render_doc` writes
"tailscale0 + RFC1918 only (enforced; public refused)" into the client-facing
/etc/catena/public-ports.md for a host on no tailnet, promising a restriction to
a network it is not on.

WHY THE ANSWER IS A PARAMETER. `rule_plan` keeps the plan as DATA so the mapping
is unit-tested without root, and the resolution comes from the store's declared
access method rather than from the live interface. A host that declares a
tailnet and fails to bring it up would otherwise silently downgrade the document
to RFC1918 while its declared posture still reads tailnet -- the quiet
divergence `rules_unapplied` exists to prevent, one layer up.

Run: uv run pytest tests/unit/test_public_ports_private_scope.py
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ANSIBLE_DIR = Path(__file__).resolve().parents[2]
HELPERS = ANSIBLE_DIR / "helpers"
SCRIPT = ANSIBLE_DIR / "scripts" / "catena-public-ports.py"

sys.path.insert(0, str(HELPERS))
import public_ports as pp  # noqa: E402


def _private(port: int = 9000, bind: str = "docker"):
    return pp.normalize_infra([
        {"proto": "tcp", "port": port, "scope": "private", "bind": bind,
         "owner": "portainer", "comment": "control-plane UI -- never public"},
    ])


def _reconciler():
    spec = importlib.util.spec_from_file_location("catena_public_ports", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_private_is_a_scope_and_survives_the_round_trip():
    """The reconciler writes the effective set and reads it back next cycle. A
    scope that did not round-trip would degrade to the `any` default and open
    the port."""
    assert "private" in pp.VALID_SCOPES
    entries = _private()
    assert pp.from_effective_json(pp.to_effective_json(entries)) == entries


def test_a_tailnet_host_gets_the_tailscale0_rule():
    plan = pp.rule_plan(_private(), tailnet_available=True)
    assert any(r.get("iface") == "tailscale0" for r in plan)


def test_a_host_with_no_tailnet_gets_rfc1918_only():
    """The rule set a `tailnet` declaration would have emitted is not wrong on
    such a host, it is just unreachable -- an interface rule that matches
    nothing. Emitting only what can match keeps the plan and the document
    describing the same firewall."""
    plan = pp.rule_plan(_private(), tailnet_available=False)
    assert not any(r.get("iface") for r in plan)
    assert {r.get("from") for r in plan if r["engine"] == "ufw"} == {
        "172.16.0.0/12", "10.0.0.0/8"}
    # Still closed on the DNAT path, which is the layer that actually guards a
    # docker-published port.
    assert [r["action"] for r in plan if r["engine"] == "docker-user"][-1] == "DROP"


def test_the_document_names_the_path_this_host_actually_has():
    """The client-facing artifact. Promising a tailscale0 restriction on a host
    with no tailnet is the defect this scope exists to end."""
    assert "tailnet" in pp.render_doc(_private(), tailnet_available=True)
    doc = pp.render_doc(_private(), tailnet_available=False)
    assert "| rfc1918 |" in doc
    assert "| tailnet |" not in doc


def test_an_explicit_tailnet_declaration_is_left_alone():
    """The migration lane binds the tailnet address and refuses to start
    without one, so it means the word. Resolution must not rewrite a caller
    that already answered the question."""
    lane = pp.normalize_infra([
        {"proto": "tcp", "port": 7701, "scope": "tailnet", "bind": "host",
         "owner": "catena-admin"},
    ])
    for available in (True, False):
        assert pp.resolve_scopes(lane, tailnet_available=available) == lane


def test_a_private_port_is_never_in_either_public_list():
    """Whichever way it resolves. An external scan must not start expecting it,
    and validate's host-bind allowlist must still carry it -- the port IS
    bound, just firewalled."""
    entries = _private()
    assert pp.expected_open_ports(entries) == []
    assert pp.public_open_tcp_ports(entries) == []
    assert pp.bound_tcp_ports(entries) == [9000]


# --- the reconciler reads the DECLARED method, not the live interface --------

def _store(tmp_path: Path, config: dict | None) -> str:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"secrets": {}, "config": config or {}}))
    return str(path)


def test_the_reconciler_reads_the_declared_access_method(tmp_path):
    mod = _reconciler()
    assert mod.tailnet_available(_store(tmp_path, {"ACCESS_METHOD": "tailnet"}))
    assert not mod.tailnet_available(
        _store(tmp_path, {"ACCESS_METHOD": "public_ssh"}))


def test_an_unreadable_store_reads_as_a_tailnet_host(tmp_path):
    """Every host installed before the key existed is on a tailnet, and the
    tailscale0 rule is the safer of the two answers to guess: on a host without
    the interface it matches nothing rather than opening anything."""
    mod = _reconciler()
    assert mod.tailnet_available(str(tmp_path / "absent.json"))
    assert mod.tailnet_available(_store(tmp_path, {}))
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    assert mod.tailnet_available(str(broken))


def test_the_two_ui_ports_declare_private_and_the_lane_declares_tailnet():
    """The declarations themselves, because the distinction only pays off if
    the callers make it. A UI port that went back to `tailnet` would start
    overstating its restriction again on a tailnet-free host."""
    portainer = (ANSIBLE_DIR / "reconcile" / "roles" / "portainer" / "tasks"
                 / "main.yml").read_text(encoding="utf-8")
    assert '"scope": "private", "bind": "docker", "owner": "portainer"' in portainer

    admin = (ANSIBLE_DIR / "bootstrap" / "roles" / "catena_admin_host" / "tasks"
             / "main.yml").read_text(encoding="utf-8")
    assert '"port": catena_admin_ui_port | int,\n           "scope": "private"' in admin
    assert '"port": catena_migrate_lane_port | int,\n           "scope": "tailnet"' in admin
