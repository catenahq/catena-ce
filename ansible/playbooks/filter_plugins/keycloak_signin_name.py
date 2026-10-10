"""The sign-in page name of the Keycloak realm, and when a converge moves it.

The realm seed (reconcile/roles/keycloak/templates/realm-vps.yaml.j2) names the
sign-in page after the domain, in its two forms: displayName, and
displayNameHtml, which the login page shows. It records the name it wrote in a
realm attribute beside them. On every converge, realm_bootstrap.yml reads the
realm and asks keycloak_signin_name_sets whether the page follows the current
domain:

  - both forms still hold what the record says Catena wrote, and the domain's
    name differs: both forms and the record take the new name;
  - either form differs from the record: a person renamed the page in
    Keycloak's realm settings, and the converge leaves it, record and all.

The record is a realm attribute, so it travels with the Keycloak database
through a backup and a restore, and a realm restored on a host serving another
domain follows that domain.

A realm seeded before the record existed has none. Its name counts as Catena's
when it equals what the seed renders for the current domain, or for a host that
had no domain yet (an empty name), and the converge records it; any other name
counts as a person's. With no domain there is no name to follow, and the page
keeps the one it has.
"""

from __future__ import annotations

import json


def keycloak_signin_name_html(name):
    """The login page's form of `name`, as the seed writes it."""
    return f"<b>{name}</b>"


def _forms(name):
    return (name, keycloak_signin_name_html(name))


def keycloak_signin_name_sets(realm, name, attribute):
    """The `kcadm update realms/<realm>` arguments that move the sign-in page
    of `realm` (the realm's representation, as `kcadm get` prints it) to
    `name` and record it in the realm attribute `attribute`, or [] when the
    page keeps its name.

    Each value is a JSON string: kcadm takes a value as JSON when it parses
    as JSON and as text otherwise (ReflectionUtil.valueToJsonNode, Keycloak
    26.6), and a JSON string is the one form that always arrives as the text
    it holds."""
    name = str(name or "").strip()
    if not name:
        return []
    current = (realm.get("displayName") or "", realm.get("displayNameHtml") or "")
    recorded = (realm.get("attributes") or {}).get(attribute)
    if recorded is None:
        catenas = current in (_forms(name), _forms(""))
    else:
        catenas = current == _forms(recorded)
    if not catenas or (recorded == name and current == _forms(name)):
        return []
    sets = {
        "displayName": name,
        "displayNameHtml": keycloak_signin_name_html(name),
        f"attributes.{attribute}": name,
    }
    return [arg for key, value in sets.items() for arg in ("-s", f"{key}={json.dumps(value)}")]


class FilterModule:
    def filters(self):
        return {
            "keycloak_signin_name_html": keycloak_signin_name_html,
            "keycloak_signin_name_sets": keycloak_signin_name_sets,
        }
