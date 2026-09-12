"""Resolve the one outgoing-mail choice into the fields a mail client needs.

SMTP_PROVIDER says where mail goes -- `resend`, `brevo`, or `server` for
anything else -- and SMTP_SENDER says who it comes from. The two known relays
supply their own host and port, and Resend also supplies its username, which is
the literal string `resend` for every account. Everything else reads the fields
as given.

WHY A FILTER. Three consumers need this resolution -- reconcile/roles/keycloak
defaults, reconcile/roles/infrastructure wordpress_plugins.yml, and beszel.yml --
and all three call here, so Keycloak's password-reset mail and WordPress's
contact form land on the same relay from the same stored config.
test_smtp_resolution.py holds each consumer to it. Project rule: a non-trivial
transform gets a filter plugin and a unit test, not a copy of an expression.

The connection is always STARTTLS on the port given, never implicit TLS. That
is what realm-vps.yaml.j2 renders (starttls true, ssl false), what PocketBase
means by `tls: false` ("leaving the server to decide whether to upgrade"), and
what both known providers expect on 587. A SMTP_USE_TLS key was collected and
read by nothing for as long as it existed.
"""

from __future__ import annotations

# The providers whose host and port are constants. Anything outside this set,
# `server` included, reads the stored host/port instead.
_KNOWN = {
    "resend": {"host": "smtp.resend.com", "port": 587, "user": "resend"},
    # Brevo's SMTP login is account-specific, so it takes the stored user.
    "brevo": {"host": "smtp-relay.brevo.com", "port": 587, "user": None},
}

# Every value the choice may take. `none` and anything unrecognised resolve to
# "no mail configured" rather than to a guess: mail that is accepted and
# silently goes nowhere is harder to notice than a service that says it has no
# mail server.
_ROUTED = frozenset(_KNOWN) | {"server"}


def catena_smtp_resolve(provider, host="", port="", user="", sender="",
                        fallback_sender=""):
    """Return {enabled, host, port, user, sender} for one stored config.

    `provider` is the piped value so the call reads as
    `cfg_smtp_provider | catena_smtp_resolve(...)`.
    """
    choice = str(provider or "").strip().lower()
    resolved_sender = str(sender or "").strip() or str(fallback_sender or "").strip()

    if choice not in _ROUTED:
        return {
            "enabled": False,
            "host": "",
            "port": 587,
            "user": "",
            "sender": resolved_sender,
        }

    known = _KNOWN.get(choice)
    if known:
        return {
            "enabled": True,
            "host": known["host"],
            "port": known["port"],
            # None means "this provider does not supply one", not "blank".
            "user": known["user"] if known["user"] is not None
            else str(user or "").strip(),
            "sender": resolved_sender,
        }

    # `server`: everything as given. The port falls back to the submission
    # port rather than to 25, which is blocked outbound by most providers and
    # would fail in a way that looks like a credential problem.
    try:
        resolved_port = int(str(port).strip() or 587)
    except (TypeError, ValueError):
        resolved_port = 587
    return {
        "enabled": True,
        "host": str(host or "").strip(),
        "port": resolved_port,
        "user": str(user or "").strip(),
        "sender": resolved_sender,
    }


class FilterModule:
    def filters(self):
        return {"catena_smtp_resolve": catena_smtp_resolve}
