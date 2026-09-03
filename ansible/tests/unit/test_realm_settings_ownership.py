"""Which realm settings the converge keeps asserting, and which it seeds once.

realm-vps.yaml.j2 used to declare every login-flow knob, all eight brute-force
numbers, defaultGroups and the locale attributes unconditionally, so
keycloak-config-cli merged them back on every converge. An admin who turned
"Verify email" off, widened the lockout window for a shared terminal, added a
second default group or switched the realm to French found it undone by the
next converge, and nothing said so.

The split is the fix, and the split is what this pins:

  ASSERTED ALWAYS -- security posture this product states. An SSO tenant for one
  business does not offer public self-registration, the email IS the username so
  it cannot be duplicated or self-edited, and a public login page is
  brute-force protected.

  SEEDED ONCE -- preferences about how this client runs their own tenant,
  marker-gated exactly like the smtpServer block.

A key moving between the two groups should be a decision someone made, not a
line that drifted, so it fails here.

Run: uv run pytest tests/unit/test_realm_settings_ownership.py
"""
from __future__ import annotations

import re
from pathlib import Path

_TEMPLATE = (
    Path(__file__).resolve().parents[3]
    / "ansible" / "roles" / "keycloak" / "templates" / "realm-vps.yaml.j2"
)
_BOOTSTRAP = (
    Path(__file__).resolve().parents[3]
    / "ansible" / "roles" / "keycloak" / "tasks" / "realm_bootstrap.yml"
)

ALWAYS = (
    "registrationAllowed",
    "registrationEmailAsUsername",
    "duplicateEmailsAllowed",
    "editUsernameAllowed",
    "bruteForceProtected",
)

SEEDED_ONCE = (
    "resetPasswordAllowed",
    "rememberMe",
    "verifyEmail",
    "loginWithEmailAllowed",
    "permanentLockout",
    "maxFailureWaitSeconds",
    "minimumQuickLoginWaitSeconds",
    "waitIncrementSeconds",
    "quickLoginCheckMilliSeconds",
    "maxDeltaTimeSeconds",
    "failureFactor",
    "defaultGroups",
    "internationalizationEnabled",
    "supportedLocales",
    "defaultLocale",
)

# The structure the product depends on. NO_DELETE already protects anything an
# admin adds beside these, so asserting them costs nothing and dropping one
# would leave a realm without the tier model the access matrix reads.
STRUCTURE = ("groups", "clientScopes", "defaultDefaultClientScopes")

_GUARD_OPEN = re.compile(r"\{%\s*if\s+settings_render\s*\|\s*default\(true\)\s*%\}")
_GUARD_CLOSE = re.compile(r"\{%\s*endif\s*%\}")


def _guard_depth_at_key(text: str) -> dict[str, int]:
    """Map each top-level YAML key to the number of open `{% if %}` blocks
    around it. The template has no nested conditionals, so depth 0 means
    unconditional and depth >= 1 means guarded."""
    depth = 0
    out: dict[str, int] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if _GUARD_OPEN.search(stripped):
            depth += 1
            continue
        if stripped.startswith("{% if"):
            depth += 1
            continue
        if _GUARD_CLOSE.search(stripped):
            depth = max(0, depth - 1)
            continue
        if stripped.startswith("#") or stripped.startswith("{#") or not stripped:
            continue
        m = re.match(r"^([A-Za-z][A-Za-z0-9_]*):", line)
        if m and not line.startswith((" ", "\t")):
            out.setdefault(m.group(1), depth)
        # Realm attributes live one level in under `attributes:`; record them
        # too so the locale keys are checkable.
        m2 = re.match(r"^\s{2}([A-Za-z][A-Za-z0-9_]*):", line)
        if m2:
            out.setdefault(m2.group(1), depth)
    return out


def test_security_posture_is_asserted_on_every_converge():
    depths = _guard_depth_at_key(_TEMPLATE.read_text())
    for key in ALWAYS:
        assert key in depths, f"{key} vanished from the realm template"
        assert depths[key] == 0, (
            f"{key} moved behind a first-import guard. It is security posture: "
            "an admin who changes it must be corrected by the next converge, "
            "not left with it."
        )


def test_client_preferences_are_seeded_once_not_reasserted():
    depths = _guard_depth_at_key(_TEMPLATE.read_text())
    for key in SEEDED_ONCE:
        assert key in depths, f"{key} vanished from the realm template"
        assert depths[key] >= 1, (
            f"{key} is asserted on every converge again. That reverts a change "
            "made in the Keycloak admin UI, silently, on the next converge."
        )


def test_the_tier_structure_stays_unconditional():
    depths = _guard_depth_at_key(_TEMPLATE.read_text())
    for key in STRUCTURE:
        assert depths.get(key) == 0, (
            f"{key} is the tier model the access matrix reads; NO_DELETE already "
            "protects what an admin adds beside it, so it stays asserted."
        )


def test_the_marker_is_probed_and_written_and_dropped_post_restore():
    body = _BOOTSTRAP.read_text()
    marker = "/var/lib/catena/keycloak-realm-settings-bootstrapped"
    assert body.count(marker) >= 3, (
        "the settings marker needs all three touchpoints: probed before the "
        "render, written after a successful first import, and pre-dropped on a "
        "post-restore converge"
    )
    assert "settings_render" in body, (
        "realm_bootstrap.yml must pass settings_render into the template"
    )


def _post_restore_drop_block(body: str) -> str:
    """The pre-drop task's own text, from its name to the next task."""
    start = body.index('- name: "Keycloak realm: drop first-import markers')
    end = body.index("\n- name:", start + 1)
    return body[start:end]


def test_the_post_restore_drop_covers_every_first_import_marker():
    """Without this, a DR converge leaves no marker (the import is skipped, so
    the mark task never fires) and the NEXT ordinary converge re-seeds over the
    realm the restore just brought back.

    DERIVED, not listed. Every marker is written only on a successful import and
    the import is skipped post-restore, so every marker is absent on a restored
    host for the same reason and every one needs dropping. Naming them here by
    hand is what left keycloak-realm-admin-bootstrapped out of a two-item loop
    for as long as it existed: the next ordinary converge then rendered the
    credentials block and reset the admin password to the store's value, undoing
    a password changed in the Account Console.
    """
    body = _BOOTSTRAP.read_text()
    markers = set(re.findall(
        r"/var/lib/catena/keycloak-realm-[a-z-]+-bootstrapped", body))
    assert markers, "no first-import markers found; did they get renamed?"
    block = _post_restore_drop_block(body)
    missing = sorted(m for m in markers if m not in block)
    assert not missing, (
        f"first-import marker(s) not dropped on a post-restore converge: "
        f"{missing}. A restored host has none of these on disk, so the next "
        "ordinary converge re-seeds whatever they guard over the restored "
        "realm."
    )
