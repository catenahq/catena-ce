"""Catena's Keycloak extension is built for the Keycloak the role deploys, and
its service files name classes that exist.

The pom's keycloak.version is the version the JAR is compiled against; the
image the role pins is the one that loads it. A different major.minor is a
JAR built against an API the server does not have.

Run: uv run pytest tests/unit/test_keycloak_provider.py
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROLE = Path(__file__).resolve().parents[2] / "reconcile" / "roles" / "keycloak"
PROVIDER = ROLE / "provider"
SERVICES = PROVIDER / "src" / "main" / "resources" / "META-INF" / "services"
SOURCES = PROVIDER / "src" / "main" / "java"


def _major_minor(version: str) -> str:
    return ".".join(version.split(".")[:2])


def test_the_pom_follows_the_image_pin():
    pom = (PROVIDER / "pom.xml").read_text()
    built_for = re.search(r"<keycloak\.version>([\d.]+)</keycloak\.version>", pom)
    assert built_for, "pom.xml has no keycloak.version property"
    tag = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())["keycloak_image_tag"]
    assert _major_minor(built_for.group(1)) == _major_minor(tag), (
        f"pom keycloak.version {built_for.group(1)} vs keycloak_image_tag {tag}")


def _service_files() -> list[Path]:
    return sorted(SERVICES.iterdir())


def test_the_extension_registers_both_provider_kinds():
    assert {p.name for p in _service_files()} == {
        "org.keycloak.authentication.AuthenticatorFactory",
        "org.keycloak.events.EventListenerProviderFactory",
    }


def _provider_id(factory: str) -> str:
    source = (SOURCES / "run" / "catena" / "keycloak" / f"{factory}.java").read_text()
    found = re.search(r'PROVIDER_ID = "([^"]+)"', source)
    assert found, f"{factory} declares no PROVIDER_ID"
    return found.group(1)


def test_the_realm_file_denies_with_the_refusal_steps_id():
    refusal = _provider_id("CatenaTierRefusalAuthenticatorFactory")
    realm = (ROLE / "templates" / "realm-vps.yaml.j2").read_text()
    steps = re.findall(r"^\s*- authenticator: (\S*(?:deny|refusal)\S*)$", realm, re.M)
    assert steps and set(steps) == {refusal}


def test_the_compose_sets_the_binding_listeners_option_by_its_id():
    binding = _provider_id("CatenaTierBindingListenerFactory")
    source = (SOURCES / "run" / "catena" / "keycloak" / "CatenaTierBindingListenerFactory.java").read_text()
    assert 'EXEMPT_CLIENT = "exemptClient"' in source
    compose = (ROLE / "templates" / "keycloak.compose.yml.j2").read_text()
    assert f"--spi-events-listener--{binding}--exempt-client=" in compose


@pytest.mark.parametrize("service", _service_files(), ids=lambda p: p.name)
def test_every_service_entry_names_an_existing_class(service):
    entries = [line.strip() for line in service.read_text().splitlines()
               if line.strip() and not line.startswith("#")]
    assert entries, f"{service.name} names no class"
    for fqcn in entries:
        source = SOURCES / (fqcn.replace(".", "/") + ".java")
        assert source.is_file(), f"{service.name}: {fqcn} has no {source.relative_to(PROVIDER)}"
