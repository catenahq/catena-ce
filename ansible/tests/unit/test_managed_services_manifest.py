"""The manifest of what the on-host update lane is allowed to bump.

A service reaches the lane one of two ways: dynamic discovery, or this explicit
manifest. `docker stack deploy` services fall outside the dynamic path, so one
missing from the manifest is one the lane never bumps -- and the lane runs,
finds nothing to do for it, and reports that as a clean run. Indistinguishable
from "nothing needed bumping".

What matters per entry: it names a swarm service that actually exists, and its
image resolves through the pin filter. An entry naming a service that is not
there is a silent skip; an entry whose image the converge asserts is a bump the
next converge undoes. The image resolves through the pin filter
(playbooks/filter_plugins/image_pin.py) or, for cloudflared, through the tunnel
engine's own max(default, pin) (catena-admin payload/cmd/catena-cloudflared-sync).
"""
from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from jinja2 import Environment

ANSIBLE = Path(__file__).resolve().parents[2]
MANIFEST = (ANSIBLE / "reconcile" / "roles" / "catena-admin" / "templates"
            / "managed-services.json.j2")


def _role_vars() -> dict:
    """Every default and var the manifest can reference.

    Role defaults leak into the play scope for roles that run later, which is
    how this template already reads traefik_container_name. Collect them the same way."""
    out: dict = {}
    for path in sorted(ANSIBLE.glob("*/roles/*/defaults/main.yml")):
        out.update(yaml.safe_load(path.read_text()) or {})
    for path in sorted(ANSIBLE.glob("*/roles/*/vars/main.yml")):
        out.update(yaml.safe_load(path.read_text()) or {})
    return out


@pytest.fixture(scope="module")
def manifest() -> list[dict]:
    body = MANIFEST.read_text()
    env = Environment()
    # The referenced names are plain strings; anything Jinja-valued in the
    # defaults resolves to its raw template text, which is fine here because
    # the manifest only reads the string-valued ones.
    rendered = env.from_string(body).render(**_role_vars())
    return json.loads(rendered)


@pytest.fixture(scope="module")
def oc():
    """helpers/onbox_config.py, which names the store's secrets."""
    spec = importlib.util.spec_from_file_location("onbox_config", ANSIBLE / "helpers" / "onbox_config.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_manifest_renders_and_is_a_list_of_specs(manifest):
    assert isinstance(manifest, list) and manifest, "no specs at all"
    for spec in manifest:
        assert spec["kind"] == "infra-swarm"
        assert spec["swarm_service"], spec
        assert spec["policy"] in ("patch", "minor", "major"), spec


def test_every_service_moves_at_patch_level_only_but_cloudflared(manifest):
    """Minors and majors arrive with a catena-admin release. catena-postgres
    numbers its releases major.minor, so its patch release is its minor, and
    "minor" is the policy that keeps its major. cloudflared keeps no data and an
    out-of-date tunnel is the larger risk, so it takes any newer release."""
    for spec in manifest:
        want = {"catena-postgres": "minor", "cloudflared": "major"}.get(spec["name"], "patch")
        assert spec["policy"] == want, spec


def test_cloudflared_is_a_service_the_engine_creates_judged_by_its_edge_connection(manifest):
    spec = next(s for s in manifest if s["name"] == "cloudflared")
    assert spec["swarm_service"] == "cloudflared"
    assert "compose_name" not in spec
    assert spec["upstream"]["source"] == "dockerhub"
    assert spec["upstream"]["repo"] == "cloudflare/cloudflared"
    # The metrics address is the one the connector is started with (catena-admin
    # payload/engines/cloudflared MetricsAddr).
    assert spec["ready"] == ["cloudflared", "tunnel", "--metrics", "127.0.0.1:20241", "ready"]


def test_every_entry_asks_whether_catena_can_still_use_it(manifest):
    """A move whose check never passes rolls back and is quarantined, so every
    entry carries one: a list of words the lane runs in the new task."""
    for spec in manifest:
        ready = spec.get("ready")
        assert isinstance(ready, list) and ready, spec
        assert all(isinstance(w, str) and w for w in ready), spec


def test_catena_postgres_answers_a_query_in_keycloaks_database(manifest):
    v = _role_vars()
    spec = next(s for s in manifest if s["name"] == "catena-postgres")
    assert spec["ready"] == ["psql", "-U", v["catena_postgres_superuser"],
                             "-d", v["keycloak_db_name"], "-tAc", "SELECT 1"]
    assert "ready_secrets" not in spec


def test_traefik_serves_keycloaks_route_on_the_web_entrypoint(manifest):
    """The route the converge writes for Keycloak's public host
    (reconcile/roles/keycloak/tasks/deploy.yml svc_domain_host), asked on the
    entrypoint's port inside the proxy's own task."""
    v = _role_vars()
    spec = next(s for s in manifest if s["name"] == v["traefik_container_name"])
    assert spec["ready"] == ["wget", "-q", "-O", "/dev/null", "-T", "10",
                             "--header", f"Host: {v['keycloak_hostname']}",
                             "http://127.0.0.1/realms/master"]
    assert "ready_secrets" not in spec


def test_keycloak_is_judged_by_the_automation_client_signing_in(manifest, oc):
    """The converge's own sign-in: the automation client reads the master realm
    through the admin API. Its secret comes from the store through the check's
    environment (KC_CLI_CLIENT_SECRET, which kcadm reads), and no argv word
    carries a credential."""
    v = _role_vars()
    spec = next(s for s in manifest if s["name"] == "keycloak")
    client = v["keycloak_automation_client_id"]
    assert spec["ready"] == [
        "/opt/keycloak/bin/kcadm.sh", "get", "realms/master", "--fields", "realm", "--no-config",
        "--server", f"http://localhost:{v['keycloak_internal_port']}", "--realm", "master",
        "--client", client, "--alias", client]
    assert spec["ready_secrets"] == {"KC_CLI_CLIENT_SECRET": "keycloak_automation_client_secret"}
    assert "keycloak_automation_client_secret" in oc.INTERNAL_SECRETS
    assert not {"--secret", "--password", "--user"} & set(spec["ready"])


def test_cloudflared_admits_only_release_tags(manifest):
    pattern = next(s for s in manifest
                   if s["name"] == "cloudflared")["upstream"]["tag_pattern"]
    for tag in ("2026.9.1", "2026.10.3", "2027.1.12"):
        assert re.fullmatch(pattern, tag), tag
    for tag in ("2026.10.3-amd64", "1980-de6376c", "latest", "2026.10", "2026.10.3.1"):
        assert not re.fullmatch(pattern, tag), tag


def test_catena_postgres_is_created_by_the_tier1_catalog_not_a_stack(manifest):
    spec = next(s for s in manifest if s["name"] == "catena-postgres")
    assert spec["swarm_service"] == "catena-postgres"
    assert "compose_name" not in spec
    assert spec["upstream"]["source"] == "dockerhub"
    assert spec["upstream"]["repo"] == "library/postgres"
    assert spec["upstream"]["scheme"] == "partial_line"


def test_catena_postgres_admits_only_trixie_minors_of_its_major(manifest):
    pattern = next(s for s in manifest
                   if s["name"] == "catena-postgres")["upstream"]["tag_pattern"]
    for tag in ("18.6-trixie", "18.7-trixie", "18.10-trixie"):
        assert re.fullmatch(pattern, tag), tag
    for tag in ("19.0-trixie", "18.6", "18.6-alpine", "18.6-bookworm", "18",
                "18.7.1-trixie", "18.6-trixie-slim", "latest"):
        assert not re.fullmatch(pattern, tag), tag


def test_every_swarm_service_name_is_fully_resolved(manifest):
    """An unresolved {{ }} means a variable the manifest cannot see from
    reconcile/roles/catena-admin, and the lane would log 'not inspectable; skipping' --
    a silent no-op that reads exactly like a healthy run."""
    for spec in manifest:
        assert "{{" not in spec["swarm_service"], spec
        assert re.fullmatch(r"[A-Za-z0-9_.\-]+", spec["swarm_service"]), spec


def test_the_stack_deployed_services_use_the_swarm_naming_convention(manifest):
    """`docker stack deploy` names services <stack>_<compose service>. A spec
    naming the STACK instead would inspect nothing."""
    by_name = {s["name"]: s["swarm_service"] for s in manifest}
    for name, stack, service in (
        ("keycloak", "keycloak", "server"),
    ):
        assert by_name.get(name) == f"{stack}_{service}", (
            f"{name} -> {by_name.get(name)!r}, want {stack}_{service}")


def test_keycloak_is_covered(manifest):
    """The services of the identity plane that are stacks of this tree."""
    names = {s["name"] for s in manifest}
    assert "keycloak" in names, f"keycloak is unmanaged again: {sorted(names)}"


def test_the_default_apps_are_updated_through_their_folders(manifest):
    """Gatus, Healthchecks and Beszel are catalog apps: the update engine
    bumps them from their folders, so a second path here would bump them twice."""
    names = {s["name"] for s in manifest}
    for absent in ("gatus", "healthchecks", "beszel", "beszel-hub", "beszel-agent"):
        assert absent not in names, f"{absent} is bumped as an infra stack and as an app"


def test_every_entry_declares_a_reachable_upstream(manifest):
    """A spec with no upstream logs 'no upstream tags available; skipping' and
    the service is never bumped, which is the state this file exists to end."""
    for spec in manifest:
        up = spec.get("upstream") or {}
        assert up.get("source") in ("dockerhub", "github", "quay", "ghcr"), spec
        assert up.get("repo"), spec
        pattern = up.get("tag_pattern")
        assert pattern, spec
        re.compile(pattern)


def test_the_declared_tag_patterns_match_the_pins_the_converge_ships(manifest):
    """The pattern has to match the tag the host is actually running, or every
    candidate is filtered out and the bump silently never happens."""
    v = _role_vars()
    for name, tag in (
        # The tier-1 catalog's PostgresImage (catena-admin
        # payload/engines/tier1/catalog.go) is not a variable of this tree.
        ("catena-postgres", "18.6-trixie"),
        # cloudflared's default is the tunnel engine's defaultImage (catena-admin
        # payload/cmd/catena-cloudflared-sync/main.go).
        ("cloudflared", "2026.9.1"),
        ("keycloak", v["keycloak_image_tag"]),
    ):
        spec = next(s for s in manifest if s["name"] == name)
        pattern = spec["upstream"]["tag_pattern"]
        assert re.fullmatch(pattern, str(tag)), (
            f"{name}: the shipped pin {tag!r} does not match this spec's "
            f"tag_pattern {pattern!r}, so no upstream tag will ever be "
            "considered comparable to it")


def test_stack_deployed_entries_declare_their_stack(manifest):
    """The engine builds its client-walk skip set from compose_name. Without
    it, a stack of Catena's own would be bumped twice: once as infra and once
    as if a client had chosen it."""
    for spec in manifest:
        if "_" not in spec["swarm_service"]:
            continue  # created directly by the converge; not a stack
        stack = spec["swarm_service"].rsplit("_", 1)[0]
        assert spec.get("compose_name") == stack, (
            f"{spec['name']} is deployed as stack {stack!r} but declares "
            f"compose_name={spec.get('compose_name')!r}")


def test_a_partial_tag_declares_its_scheme(manifest):
    """catena-postgres ships X.Y. Without scheme=partial_line the engine
    classifies the running tag as not-full-semver and skips it."""
    pg = next(s for s in manifest if s["name"] == "catena-postgres")
    assert pg["upstream"].get("scheme") == "partial_line"


def test_keycloak_is_ordered_by_its_builds(manifest):
    """Keycloak is pinned to an immutable <version>.<unix-build-time> build.
    Without scheme=build_suffix the engine compares only three parts, so a
    rebuild never ranks above the running build."""
    kc = next(s for s in manifest if s["name"] == "keycloak")
    assert kc["upstream"].get("scheme") == "build_suffix"


def test_keycloak_admits_every_build_tag(manifest):
    """Every build of every Keycloak version is a candidate; a bare version is not."""
    pattern = next(s for s in manifest
                   if s["name"] == "keycloak")["upstream"]["tag_pattern"]
    for tag in ("26.8.1.1793773817", "26.8.2.1793909084", "26.9.0.1"):
        assert re.fullmatch(pattern, tag), tag
    for tag in ("26.8.2", "26.8", "latest"):
        assert not re.fullmatch(pattern, tag), tag


def test_nothing_carries_a_copy_of_how_the_service_was_built(manifest):
    for spec in manifest:
        assert "run_argv" not in spec
        assert "container_name" not in spec


def test_the_exclusions_are_written_down():
    """Each absent service is absent for a reason. An undocumented gap reads as
    an oversight and gets 'fixed' by adding a service that must not be there --
    the panel, which cannot roll itself with a plain service update."""
    header = MANIFEST.read_text().split("#}")[0]
    name = MANIFEST.parent.parent.name
    assert name in header, f"{name} is absent from the manifest silently"


def test_each_declared_gate_has_a_patch_spec(manifest):
    v = _role_vars()
    gates = [s for s in manifest if s.get("compose_name") == v["oauth2_proxy_compose_name"]]
    assert {s["name"] for s in gates} == {
        f"{v['oauth2_proxy_compose_name']}_oauth2-proxy-{app['slug']}"
        for app in v["oauth2_proxy_apps"]
    }
    for spec in gates:
        assert spec["swarm_service"] == spec["name"]
        assert spec["policy"] == "patch"
        assert spec["upstream"]["source"] == "quay"
        assert spec["upstream"]["repo"] == "oauth2-proxy/oauth2-proxy"
        pattern = spec["upstream"]["tag_pattern"]
        assert re.fullmatch(pattern, v["oauth2_proxy_image_tag"])
        for tag in ("v7.15.7-alpine", "v8.0.0-alpine"):
            assert re.fullmatch(pattern, tag)
        for tag in ("latest", "v7.15.7", "v7.15.7-amd64", "v7.15.7-alpine-extra"):
            assert not re.fullmatch(pattern, tag)
        assert spec["ready"][:2] == ["sh", "-c"]
        assert f"nc -w 5 127.0.0.1 {v['oauth2_proxy_internal_port']}" in spec["ready"][2]
    header = MANIFEST.read_text().split("#}", 1)[0]
    exclusions = header.split("The services this manifest leaves out", 1)[1]
    assert "oauth2-proxy" not in exclusions


@pytest.mark.parametrize("reply,ok", [
    ("HTTP/1.1 302 Found\r\nLocation: https://auth.example/auth\r\n\r\n", True),
    ("HTTP/1.1 500 Internal Server Error\r\n\r\n", False),
    ("HTTP/1.1 302 Found\r\nLocation: /elsewhere\r\n\r\n", False),
    ("", False),
])
def test_gate_ready_requires_a_sign_in_redirect(manifest, tmp_path, monkeypatch, reply, ok):
    sh = shutil.which("sh")
    if not sh:
        pytest.skip("sh unavailable")
    nc = tmp_path / "nc"
    nc.write_text("#!/bin/sh\nwhile IFS= read -r line; do :; done\nprintf '%s' \"$GATE_REPLY\"\n")
    nc.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("GATE_REPLY", reply)
    spec = next(s for s in manifest if s.get("compose_name") == _role_vars()["oauth2_proxy_compose_name"])
    result = subprocess.run([sh, *spec["ready"][1:]], capture_output=True, timeout=5)
    assert (result.returncode == 0) is ok


def test_validate_refuses_missing_duplicate_or_unsafe_gate_specs(manifest):
    from ansible_jinja import holds

    tasks = yaml.safe_load((MANIFEST.parent.parent / "tasks" / "validate.yml").read_text())
    task = next(t for t in tasks if "each declared gate" in t.get("name", ""))
    v = _role_vars()
    spec = next(s for s in manifest if s.get("compose_name") == v["oauth2_proxy_compose_name"])
    ctx = {"_ca_val_gate_name": spec["name"], "_ca_val_gate_compose": v["oauth2_proxy_compose_name"]}
    key = "ansible.builtin.assert"
    assert holds(task, key, _ca_val_gate=[spec], **ctx)
    assert not holds(task, key, _ca_val_gate=[], **ctx)
    assert not holds(task, key, _ca_val_gate=[spec, spec], **ctx)
    for field, value in (("kind", "client"), ("policy", "major"), ("ready", []),
                         ("ready", ["true"]), ("swarm_service", "absent"),
                         ("upstream", {"source": "quay", "repo": "wrong/repo"})):
        assert not holds(task, key, _ca_val_gate=[{**spec, field: value}], **ctx)


def test_validate_loads_the_gate_list_itself():
    """playbooks/validate.yml includes each role on its own, so the catena-admin
    validate sees none of reconcile/roles/oauth2_proxy's defaults it does not load."""
    tasks = yaml.safe_load((MANIFEST.parent.parent / "tasks" / "validate.yml").read_text())
    load = next(t for t in tasks if "ansible.builtin.include_vars" in t)
    assert load["ansible.builtin.include_vars"]["file"].endswith("/oauth2_proxy/defaults/main.yml")
    task = next(t for t in tasks if "each declared gate" in t.get("name", ""))
    used = yaml.safe_dump({k: task[k] for k in ("ansible.builtin.assert", "vars", "loop")})
    assert not re.search(r"(?<![\w.])oauth2_proxy_\w+", used)
