"""The sign-in realm's mail settings, and when a converge writes them.

Settings > Outgoing mail decides what the realm (reconcile/roles/keycloak/
templates/realm-vps.yaml.j2) carries as `smtpServer`. The realm records what
Catena last wrote in a realm attribute: a fingerprint of the mail fields that
Keycloak returns. Keycloak never returns the stored password, so it is not part
of the fingerprint. realm_bootstrap.yml reads the realm before its imports and
asks keycloak_mail_follows whether the import carries the mail settings:

  - the realm holds no mail settings, or no record: they are written, and the
    record follows the import, so a realm Catena has not recorded yet is
    adopted whatever it holds;
  - the realm holds a record: they are written while the realm's settings still
    match the record, and a difference is a hand edit in the realm's Email tab,
    which the import leaves alone.

After the imports, keycloak_mail_record_sets gives the kcadm arguments that
record the fingerprint once the realm holds what Catena would write. The record
is a realm attribute, so it travels with the Keycloak database through a backup
and a restore.

A hand edit that changes only the password cannot be seen: the other fields
still match the record, and the next converge writes the panel's password.
"""

from __future__ import annotations

import hashlib
import json

FIELDS = ("host", "port", "from", "fromDisplayName", "replyTo", "replyToDisplayName",
          "envelopeFrom", "user", "auth", "ssl", "starttls")
FLAGS = ("auth", "ssl", "starttls")


def _project(smtp):
    """The compared fields of an `smtpServer` map, as text, empty ones left out."""
    out = {}
    for key in FIELDS:
        value = (smtp or {}).get(key)
        text = "" if value is None else str(value).strip()
        if key in FLAGS:
            text = text.lower()
        if text:
            out[key] = text
    return out


def keycloak_mail_fingerprint(smtp):
    """A fingerprint of the compared fields of the `smtpServer` map `smtp`."""
    canonical = json.dumps(_project(smtp), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def keycloak_mail_follows(realm, attribute):
    """Whether the import writes the mail settings Catena renders into `realm`
    (its representation as `kcadm get` prints it, or empty for a realm that
    does not exist yet), with the record in the realm attribute `attribute`."""
    realm = realm or {}
    current = _project(realm.get("smtpServer"))
    recorded = (realm.get("attributes") or {}).get(attribute)
    if not current or recorded is None:
        return True
    return keycloak_mail_fingerprint(current) == recorded


def keycloak_mail_record_sets(realm, wanted, attribute):
    """The `kcadm update realms/<realm>` arguments that record the fingerprint
    of `wanted` in the realm attribute `attribute`, or [] when `realm` (read
    after the import) does not hold what `wanted` says or already records it.

    The value is a JSON string: kcadm takes a value as JSON when it parses as
    JSON and as text otherwise, and a JSON string always arrives as the text it
    holds."""
    wanted_print = keycloak_mail_fingerprint(wanted)
    if keycloak_mail_fingerprint(realm.get("smtpServer")) != wanted_print:
        return []
    if (realm.get("attributes") or {}).get(attribute) == wanted_print:
        return []
    return ["-s", f"attributes.{attribute}={json.dumps(wanted_print)}"]


class FilterModule:
    def filters(self):
        return {
            "keycloak_mail_follows": keycloak_mail_follows,
            "keycloak_mail_record_sets": keycloak_mail_record_sets,
        }
