"""A loopback-scoped port must be DENIED off-box, and must never be allowed.

Swarm cannot publish to 127.0.0.1: its PortConfig carries no host IP, so the
loopback binds that compose gave Gatus, Healthchecks and the Beszel hub become
host-wide binds once those services move to `docker stack deploy`. The ports
cannot simply be dropped -- gatus-sync, the clamav watchdog, the mail canary,
beszel-seed and the host-network Beszel agent are all HOST processes that dial
them. `scope: loopback` is what keeps the old posture: the box reaches the
port, nothing else does.

The failure this pins is specific and silent. `_ufw_spec` used to hardcode
"allow" as the first word of the spec. Feed it a deny rule and it does not
merely fail to guard the port -- it runs `ufw allow` on the port it was asked
to close, records that as applied, and every artifact validation reads then
says the host is guarded.

Run: uv run pytest tests/unit/test_public_ports_loopback_scope.py
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ANSIBLE_DIR = Path(__file__).resolve().parents[2]
HELPERS = ANSIBLE_DIR / "helpers"
SCRIPT = ANSIBLE_DIR / "scripts" / "catena-public-ports.py"

sys.path.insert(0, str(HELPERS))
import public_ports as pp  # noqa: E402


def _entry(bind: str = "host", proto: str = "tcp", port: int = 9021):
    return pp.normalize_infra([
        {"proto": proto, "port": port, "scope": "loopback", "bind": bind,
         "owner": "gatus", "comment": "host-side gatus-sync only"},
    ])[0]


def _reconciler():
    spec = importlib.util.spec_from_file_location("catena_public_ports", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- the scope exists and survives a round trip ---------------------------

def test_loopback_is_a_valid_scope():
    assert "loopback" in pp.VALID_SCOPES
    assert _entry().scope == "loopback"


def test_survives_the_effective_json_round_trip():
    # The reconciler writes the effective set and reads it back on the next
    # cycle; a scope that did not round-trip would silently degrade to the
    # `any` default and open the port.
    entries = [_entry()]
    assert pp.from_effective_json(pp.to_effective_json(entries)) == entries


# --- the ufw layer denies, and denies ONLY --------------------------------

def test_host_bind_emits_exactly_one_deny_and_no_allow():
    plan = pp.rule_plan([_entry(bind="host")])
    assert [r["action"] for r in plan] == ["deny"]
    assert plan[0]["engine"] == "ufw"
    assert plan[0]["port"] == "9021"


def test_docker_bind_adds_a_dnat_drop_with_no_allowed_source():
    # This is the bind the three real loopback ports use, and the DROP is the
    # only rule that actually closes them. A swarm `mode: host` publish is
    # still DNAT'd, so it bypasses ufw INPUT exactly like an ordinary
    # published port: declared bind=host, the ufw deny installs cleanly and
    # the port stays reachable off-box (bench 050b found 18000, 18080 and
    # 18190 open on the bridge IP). Unlike tailnet/rfc1918 there is no source
    # to RETURN first -- anything reaching that chain came from off-box,
    # since 127.0.0.1 is delivered on loopback and never traverses FORWARD.
    plan = pp.rule_plan([_entry(bind="docker")])
    assert [(r["engine"], r["action"]) for r in plan] == [
        ("ufw", "deny"),
        ("docker-user", "DROP"),
    ]
    assert not any(r["action"] == "RETURN" for r in plan)


# --- the port lists validation reads --------------------------------------

def test_bound_but_not_public():
    entries = [_entry()]
    # validate.yml's host-bind drift check allowlists every BOUND tcp port,
    # and a swarm host-mode publish is bound -- so it must appear here or the
    # converge fails on its own service.
    assert pp.bound_tcp_ports(entries) == [9021]
    # ...and must NOT appear in either public expectation, or the external
    # nmap would start demanding an open port.
    assert pp.expected_open_ports(entries) == []
    assert pp.public_open_tcp_ports(entries) == []


def test_generated_inventory_explains_the_scope():
    doc = pp.render_doc([_entry()])
    assert "loopback" in doc
    assert "9021" in doc


# --- the reconciler renders the action it was given -----------------------

def test_ufw_spec_renders_deny_not_allow():
    mod = _reconciler()
    rule = pp.rule_plan([_entry(bind="host")])[0]
    assert mod._ufw_spec(rule)[0] == "deny"
    # The full argv is what actually runs; a stray "allow" anywhere in it
    # would open the port.
    assert "allow" not in mod._ufw_argv(rule)


def test_the_three_real_loopback_ports_declare_the_dnat_bind():
    """The regression this pins is silent in the worst way: declared
    bind=host, ufw installs a deny, `rules_unapplied` is 0, every artifact
    validation reads says the port is guarded -- and the port answers from
    off-box. Only an external scan sees it, which is what caught it."""
    tasks_dir = ANSIBLE_DIR / "roles" / "infrastructure" / "tasks"
    for name in ("gatus.yml", "healthchecks.yml", "beszel.yml"):
        body = (tasks_dir / name).read_text(encoding="utf-8")
        assert '"scope": "loopback", "bind": "docker"' in body, (
            f"{name}: the loopback port must declare bind=docker. A swarm "
            f"mode:host publish is DNAT'd and never reaches ufw INPUT, so a "
            f"ufw deny does not close it -- the guard is DOCKER-USER."
        )
        assert '"bind": "host"' not in body, (
            f"{name}: bind=host claims ufw INPUT can enforce this port"
        )


def test_ufw_spec_still_defaults_to_allow_for_the_existing_scopes():
    mod = _reconciler()
    for scope in ("any", "tailnet", "rfc1918"):
        entries = pp.normalize_infra([
            {"proto": "tcp", "port": 5349, "scope": scope, "bind": "host"},
        ])
        for rule in pp.rule_plan(entries):
            if rule["engine"] == "ufw":
                assert mod._ufw_spec(rule)[0] == "allow"
