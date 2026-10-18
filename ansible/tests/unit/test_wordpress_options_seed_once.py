"""Who owns each WordPress plugin option the converge writes.

wordpress_plugins.yml writes rows in wp_options that the WordPress admin UI
also writes, and Ansible's `combine` lets its RIGHT-hand side win. The split
this pins:

  SEEDED ONCE PER SITE, then the site admin's -- the NPP cache settings, on the
  first converge after the install wizard, and WP Mail SMTP's sender (from
  address and name, their force flags, the return path), on the first converge
  with outgoing mail on, which can come later. Each seed goes UNDER what the
  site already carries and has its own marker, keyed on the site's stack.

  PUT BACK ON EVERY CONVERGE WITH MAIL ON -- WP Mail SMTP's relay (mailer,
  host, port, user, encryption, auth), which Settings > Outgoing mail owns. The
  password is Catena's, so a relay the site kept from another provider would
  send the new password to the old provider.

  WITH MAIL OFF, nothing is written to wp_mail_smtp and no mail marker is.

Each is one operand order, one marker key or one `when` away from silently
regressing, and no other test would notice.

Run: uv run pytest tests/unit/test_wordpress_options_seed_once.py
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.plugins.loader import add_all_plugin_dirs
from ansible.template import Templar, trust_as_template

_ANSIBLE = Path(__file__).resolve().parents[2]
_ROLE = _ANSIBLE / "reconcile" / "roles" / "infrastructure"
_TASKS = _ROLE / "tasks" / "wordpress_plugins.yml"
_DEFAULTS = _ROLE / "defaults" / "main.yml"

add_all_plugin_dirs(str(_ANSIBLE / "playbooks"))

MARKER_PREFIX = "/var/lib/catena/wordpress-options-bootstrapped-"
MAIL_MARKER_PREFIX = "/var/lib/catena/wordpress-mail-bootstrapped-"

RESEND = {"provider": "resend", "sender": "no-reply@acme.test"}
BREVO = {"provider": "brevo", "user": "8a1b2c@smtp-brevo.com", "sender": "no-reply@acme.test"}


def _tasks() -> list[dict]:
    """Every task in the file, flattened out of its blocks."""
    out: list[dict] = []

    def walk(items):
        for t in items or []:
            out.append(t)
            for key in ("block", "always", "rescue"):
                if key in t:
                    walk(t[key])

    walk(yaml.safe_load(_TASKS.read_text()))
    return out


def _by_name(fragment: str) -> dict:
    for t in _tasks():
        if fragment in (t.get("name") or ""):
            return t
    raise AssertionError(f"no task whose name contains {fragment!r}")


def _uses(task: dict, name: str) -> bool:
    return name in task or f"ansible.builtin.{name}" in task


def _module(task: dict, name: str):
    """The task's arguments for `name`, whether it is written short or FQCN."""
    for key in (name, f"ansible.builtin.{name}"):
        if key in task:
            return task[key]
    raise AssertionError(f"task {task.get('name')!r} does not use {name}")


def _conditions(task: dict) -> list:
    conditions = task.get("when")
    return [conditions] if isinstance(conditions, str) else conditions or []


def _combine_expr(task: dict) -> str:
    return " ".join(str(next(iter(_module(task, "set_fact").values()))).split())


def _trusted(value):
    """YAML strings are data to Ansible's templar until marked as templates."""
    if isinstance(value, str):
        return trust_as_template(value)
    if isinstance(value, dict):
        return {k: _trusted(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_trusted(v) for v in value]
    return value


def _render(value, variables: dict):
    return Templar(loader=DataLoader(), variables=variables).template(_trusted(value))


def _runs(task: dict, variables: dict) -> bool:
    return all(_render("{{ " + str(c) + " }}", variables) for c in _conditions(task))


# --- NPP: seeded once ----------------------------------------------------------

def test_npp_settings_seed_under_what_the_site_already_has():
    expr = _combine_expr(_by_name("seed NPP settings under whatever"))
    assert re.search(r"_wp_npp_desired\s*\|\s*combine\(", expr), (
        "desired must be the LEFT operand: combine lets the right-hand side "
        f"win, so this order is what keeps the site's own value. Got: {expr}"
    )
    assert "_parsed" in expr


def test_every_npp_write_is_gated_on_the_seed_decision():
    """The gate is a single computed fact so the four NPP tasks cannot drift
    apart. A write keyed on _wp_installed alone runs on every converge."""
    for fragment in ("declare NPP desired settings",
                     "get current NPP settings",
                     "seed NPP settings under whatever",
                     "update NPP settings"):
        joined = " ".join(str(c) for c in _conditions(_by_name(fragment)))
        assert "_wp_seed_options" in joined, (
            f"{fragment!r} is not gated on the seed decision: {joined!r}"
        )


def test_the_npp_marker_is_written_only_on_a_run_that_seeded():
    """Unconditional, it would mark a converge that skipped the writes and no
    later converge would ever seed. Gated, an interrupted run is retried."""
    task = _by_name("mark this site's NPP settings as seeded")
    args = _module(task, "file")
    assert args["state"] == "touch"
    assert "_wp_seed_marker" in str(args["path"])
    assert "_wp_seed_options" in " ".join(str(c) for c in _conditions(task))


# --- the markers: one per seed, keyed on the site ------------------------------

@pytest.mark.parametrize("label,container,key", [
    ("catena-wordpress", "catena-wordpress_wp.1.abc", "catena-wordpress"),
    ("catena-wordpress\n", "catena-wordpress_wp.1.xyz", "catena-wordpress"),
    ("", "wordpress-abc123-wp-1", "wordpress-abc123"),
])
def test_both_markers_are_keyed_on_the_site_not_the_container(label, container, key):
    """A container name carries the swarm task id and changes on every
    reschedule. A marker keyed on that resets itself and re-seeds catena's
    values over the admin's edits."""
    task = _by_name("resolve this site's seed markers")
    variables = {"_wp_stack_name": {"stdout": label}, "_wp_ct": {"stdout": container},
                 **_trusted(task["vars"])}
    assert _render(_module(task, "set_fact"), variables) == {
        "_wp_seed_marker": MARKER_PREFIX + key,
        "_wp_mail_seed_marker": MAIL_MARKER_PREFIX + key,
    }
    resolver = _by_name("resolve this site's stack name")
    assert "com.docker.stack.namespace" in str(_module(resolver, "command")["argv"])
    assert resolver.get("changed_when") is False


# --- WP Mail SMTP: the relay follows Settings, the sender is seeded once -------

_INFRA_SMTP = yaml.safe_load(_DEFAULTS.read_text())["infrastructure_smtp"]

_STEP_7 = (
    "derive consolidated SMTP config",
    "skip wp-mail-smtp wiring",
    "decide what to write to wp_mail_smtp",
    "WP Mail SMTP sender already seeded",
    "declare wp-mail-smtp relay and sender",
    "get current wp_mail_smtp option",
    "get the site's admin email",
    "seed the sender, put the relay back",
    "update wp_mail_smtp option",
    "mark this site's WP Mail SMTP sender as seeded",
)


def _converge(site: dict | None, *, seeded: bool, provider="", user="", sender="",
              host="", port="", site_admin="admin@acme.test") -> dict:
    """Step 7 as one converge runs it on an installed site.

    `site` is the wp_mail_smtp option the site holds (None: never set),
    `seeded` whether its mail-seed marker exists, and the keywords the
    outgoing mail saved in Settings, resolved through the role's own
    infrastructure_smtp. Returns the option written (None when none is),
    whether the marker is written, and the debug tasks that spoke.
    """
    marker = MAIL_MARKER_PREFIX + "catena-wordpress"
    v = {
        "infrastructure_smtp": trust_as_template(_INFRA_SMTP),
        "cfg_smtp_provider": provider, "cfg_smtp_user": user, "cfg_smtp_sender": sender,
        "cfg_smtp_host": host, "cfg_smtp_port": port,
        "admin_email": "admin@acme.test",
        "_wp_installed": {"rc": 0},
        "_wp_ct": {"stdout": "catena-wordpress_wp.1.abc"},
        "_wp_mail_seed_marker": marker,
        "_wp_mail_seed_marker_stat": {"stat": {"exists": seeded}},
    }
    run = {"written": None, "marked": False, "said": []}
    for fragment in _STEP_7:
        task = _by_name(fragment)
        scope = {**v, **_trusted(task.get("vars") or {})}
        if not _runs(task, scope):
            continue
        if _uses(task, "set_fact"):
            v.update(_render(_module(task, "set_fact"), scope))
        elif _uses(task, "shell"):
            if task["register"] == "_wp_site_admin_email":
                v[task["register"]] = {"stdout": site_admin + "\n"}
            else:
                v[task["register"]] = {"stdout": json.dumps(site if site is not None else {})}
        elif _uses(task, "command"):
            argv = _render(_module(task, "command")["argv"], scope)
            run["written"] = json.loads(argv[argv.index("wp_mail_smtp") + 1])
        elif _uses(task, "file"):
            assert _render(_module(task, "file")["path"], scope) == marker
            run["marked"] = True
        else:
            run["said"].append(fragment)
    return run


def test_a_site_installed_before_mail_is_chosen_is_wired_once_it_is():
    """The converge after the install wizard, with no outgoing mail yet, leaves
    the mail seed to the first converge with mail on."""
    before = _converge(None, seeded=False)
    assert before["written"] is None and not before["marked"]
    assert before["said"] == ["skip wp-mail-smtp wiring"]

    after = _converge(None, seeded=False, **RESEND)
    assert after["marked"]
    assert after["written"] == {
        "mail": {"mailer": "smtp", "from_email": "no-reply@acme.test",
                 "from_email_force": True, "from_name": "no-reply@acme.test",
                 "from_name_force": False, "return_path": False},
        "smtp": {"host": "smtp.resend.com", "port": 587, "encryption": "tls",
                 "autotls": True, "auth": True, "user": "resend"},
    }


def test_the_port_decides_the_encryption():
    """465 is implicit TLS (PHPMailer's 'ssl'); every other port is STARTTLS
    ('tls')."""
    for port, encryption in (("465", "ssl"), ("587", "tls"), ("2525", "tls")):
        run = _converge(None, seeded=False, provider="server", host="mail.acme.test",
                        port=port, user="u", sender="no-reply@acme.test")
        assert run["written"]["smtp"]["port"] == int(port)
        assert run["written"]["smtp"]["encryption"] == encryption, port


def test_the_relay_follows_settings_while_the_sender_stays_the_sites():
    """Settings moves from Resend to Brevo on a site whose admin set their own
    sender and switched the mailer inside WordPress: the relay is Settings',
    the sender and every other key stay the site's, and the seed is not run
    again, so a sender key the site does not carry is not added back."""
    site = {
        "mail": {"mailer": "mail", "from_email": "shop@acme.test",
                 "from_email_force": False, "from_name": "Acme Shop",
                 "from_name_force": True},
        "smtp": {"host": "smtp.resend.com", "port": 587, "encryption": "tls",
                 "autotls": True, "auth": True, "user": "resend"},
        "logs": {"enabled": True},
    }
    run = _converge(site, seeded=True, **BREVO)
    assert not run["marked"]
    assert run["said"] == ["WP Mail SMTP sender already seeded"]
    assert run["written"] == {
        "mail": {**site["mail"], "mailer": "smtp"},
        "smtp": {"host": "smtp-relay.brevo.com", "port": 587, "encryption": "tls",
                 "autotls": True, "auth": True, "user": "8a1b2c@smtp-brevo.com"},
        "logs": {"enabled": True},
    }


def test_the_first_seed_keeps_a_sender_the_site_already_has():
    """A site whose admin filled in WP Mail SMTP by hand before mail was
    chosen keeps that sender; the seed fills only what is unset."""
    run = _converge({"mail": {"from_email": "shop@acme.test", "from_name": "Acme Shop"}},
                    seeded=False, **RESEND)
    assert run["marked"]
    mail = run["written"]["mail"]
    assert (mail["from_email"], mail["from_name"]) == ("shop@acme.test", "Acme Shop")
    assert mail["from_email_force"] is True


def test_a_sender_that_is_the_plugins_default_admin_email_is_seeded_over():
    """WP Mail SMTP stores from_email = the site's admin_email when it is
    activated, so the seed would otherwise never land. Case differences do not
    make it an edit."""
    run = _converge({"mail": {"from_email": "Admin@Acme.test", "from_name": "Acme Shop"}},
                    seeded=False, **RESEND)
    assert run["marked"]
    mail = run["written"]["mail"]
    assert mail["from_email"] == "no-reply@acme.test"
    assert mail["from_name"] == "Acme Shop"


def test_a_sender_the_admin_chose_is_kept_even_when_it_differs_from_admin_email():
    run = _converge({"mail": {"from_email": "shop@acme.test"}}, seeded=False, **RESEND)
    assert run["written"]["mail"]["from_email"] == "shop@acme.test"


def test_a_sender_equal_to_admin_email_is_kept_once_seeded():
    """After the seed the site's from_email is the admin's, whatever it reads."""
    site = {"mail": {"from_email": "admin@acme.test", "mailer": "smtp"}}
    run = _converge(site, seeded=True, **RESEND)
    assert run["written"]["mail"]["from_email"] == "admin@acme.test"


def test_a_converge_with_nothing_to_change_writes_nothing():
    first = _converge(None, seeded=False, **RESEND)["written"]
    again = _converge(first, seeded=True, **RESEND)
    assert again["written"] is None and not again["marked"]


@pytest.mark.parametrize("seeded", [False, True])
def test_mail_off_writes_nothing_and_leaves_the_site_as_it_is(seeded):
    site = {"mail": {"mailer": "smtp", "from_email": "shop@acme.test"},
            "smtp": {"host": "smtp.resend.com", "user": "resend"}}
    run = _converge(site, seeded=seeded)
    assert run["written"] is None and not run["marked"]
    assert run["said"] == ["skip wp-mail-smtp wiring"]


def test_the_option_write_stays_out_of_the_log():
    assert _by_name("update wp_mail_smtp option").get("no_log") is True


def test_plugin_activation_still_runs_on_every_converge():
    """The seed markers govern OPTIONS only. Install and activate flow forward
    and never deactivate, so they stay unconditional -- a new curated plugin
    must still land on an already-seeded site."""
    for fragment in ("install + activate any missing plugin",
                     "activate any installed-but-inactive plugin"):
        joined = " ".join(str(c) for c in _conditions(_by_name(fragment)))
        for gate in ("_wp_seed_options", "_wp_mail_seed", "_wp_mail_wire"):
            assert gate not in joined, (
                f"{fragment!r} was caught by the {gate} gate; a site seeded "
                "once would never receive a newly curated plugin again"
            )
