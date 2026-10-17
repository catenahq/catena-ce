"""The mail server's certificate and its mail DNS records follow the hostname
and mail domain its stack runs under, not the primary zone: the webmail
verifies the certificate against the stack's MAIL_HOSTNAME, and a
primary-domain change does not move the stack. A name outside every zone the
Cloudflare token reaches is not touched: the certificate stays, the records
stay, and the converge says so.

Run: uv run pytest tests/unit/test_mail_cert_follows_the_stack_hostname.py
"""
from __future__ import annotations

import yaml
from jinja2.nativetypes import NativeEnvironment

from ansible_tree import role_dir, walk_tasks

_ROLE = role_dir("infrastructure")
NAMES = yaml.safe_load((_ROLE / "tasks" / "_mailserver_names.yml").read_text())
CERT = yaml.safe_load((_ROLE / "tasks" / "mailserver_cert.yml").read_text())
DNS = yaml.safe_load((_ROLE / "tasks" / "mailserver_dns.yml").read_text())
DEFAULTS = yaml.safe_load((_ROLE / "defaults" / "main.yml").read_text())
ENV_TEMPLATE = (_ROLE / "templates" / "mailserver-cert.env.j2").read_text()


def _task(tasks, fragment: str) -> dict:
    for t in walk_tasks(tasks):
        if fragment in str(t.get("name", "")):
            return t
    raise AssertionError(f"task not found: {fragment!r}")


def _names(stack_rc, stack_lines, zone, zones_status=200, token_zones=()):
    """Run the set_fact expressions of _mailserver_names.yml in order."""
    env = NativeEnvironment()
    ctx = {
        "cloudflare_zone": zone,
        "cloudflare_zone_id": f"id-{zone}",
        "mailserver_hostname": f"mail.{zone}",
        "_ms_names_stack": {"rc": stack_rc, "stdout_lines": stack_lines},
    }
    if zones_status is not None:
        ctx["_ms_names_zones"] = {
            "status": zones_status,
            "json": {"result": [{"name": z, "id": f"id-{z}"} for z in token_zones]},
        }
    for fragment in ("choose the hostname", "find the zone", "publish the zone ids"):
        for key, expr in _task(NAMES, fragment)["ansible.builtin.set_fact"].items():
            value = env.from_string(expr).render(**ctx)
            ctx[key] = value.strip() if isinstance(value, str) else value
    return ctx


def _stack(host, domain):
    return [host, "SSL_TYPE=manual", f"POSTMASTER_ADDRESS=postmaster@{domain}", ""]


def test_names_are_the_stacks_after_a_domain_change():
    ctx = _names(0, _stack("mail.old.example", "old.example"), "new.example",
                 token_zones=("new.example",))
    assert ctx["mailserver_stack_hostname"] == "mail.old.example"
    assert ctx["mailserver_stack_domain"] == "old.example"
    assert ctx["_ms_host_reachable"] is False
    assert ctx["_ms_domain_reachable"] is False
    assert ctx["_ms_host_zone_id"] == "" and ctx["_ms_domain_zone_id"] == ""


def test_a_token_that_reaches_the_old_zone_publishes_there():
    ctx = _names(0, _stack("mail.old.example", "old.example"), "new.example",
                 token_zones=("new.example", "old.example"))
    assert ctx["_ms_host_reachable"] is True and ctx["_ms_domain_reachable"] is True
    assert ctx["_ms_host_zone_id"] == "id-old.example"
    assert ctx["_ms_domain_zone_id"] == "id-old.example"


def test_the_host_and_the_mail_domain_may_sit_in_different_zones():
    ctx = _names(0, _stack("mail.hosting.example", "old.example"), "new.example",
                 token_zones=("hosting.example",))
    assert ctx["_ms_host_zone_id"] == "id-hosting.example"
    assert ctx["_ms_domain_reachable"] is False


def test_the_longest_zone_that_holds_the_name_wins():
    ctx = _names(0, _stack("mail.eu.new.example", "new.example"), "new.example",
                 token_zones=("eu.new.example",))
    assert ctx["_ms_host_zone_id"] == "id-eu.new.example"
    assert ctx["_ms_domain_zone_id"] == "id-new.example"


def test_an_unchanged_domain_uses_the_primary_zone():
    ctx = _names(0, _stack("mail.new.example", "new.example"), "new.example")
    assert ctx["mailserver_stack_hostname"] == "mail.new.example"
    assert ctx["_ms_host_zone_id"] == "id-new.example"
    assert ctx["_ms_domain_reachable"] is True


def test_an_unreadable_stack_leaves_both_names_unknown():
    """The callers run after the service was found, so a failed inspect is an
    error: falling back to mail.<primary zone> would put the certificate and
    the records back under the zone the stack left."""
    for rc, lines in ((1, []), (1, _stack("mail.old.example", "old.example")), (0, [""])):
        ctx = _names(rc, lines, "new.example", token_zones=("new.example",))
        assert ctx["_ms_names_known"] is False
        assert ctx["mailserver_stack_hostname"] == ""
        assert ctx["mailserver_stack_domain"] == ""
        assert ctx["_ms_host_reachable"] is False
        assert ctx["_ms_domain_reachable"] is False


def test_a_readable_stack_is_known():
    ctx = _names(0, _stack("mail.old.example", "old.example"), "new.example")
    assert ctx["_ms_names_known"] is True


def test_nothing_is_issued_or_published_while_the_names_are_unknown():
    block = _task(CERT, "issue + inject")
    assert "_ms_names_known | bool" in block["when"]
    assert "_ms_names_known | bool" in _task(DNS, "report the records left alone")["when"]
    for tasks, fragment in ((CERT, "could not be read"), (DNS, "could not be read")):
        assert "not (_ms_names_known | bool)" in _task(tasks, fragment)["when"]


def test_a_certificate_that_does_not_exist_does_not_stop_the_converge():
    """A stack hostname outside the token's zones with no certificate yet: the
    report is the outcome. Only a name the token reaches must have one."""
    confirm = _task(CERT, "confirm cert is now present")
    assert confirm["failed_when"] == "(_ms_host_reachable | bool) and not _ms_cert_after.stat.exists"
    for fragment in ("enable certbot.timer", "inject cert into dms"):
        assert _task(CERT, fragment)["when"] == "_ms_cert_after.stat.exists"


def test_a_zone_listing_that_failed_leaves_only_the_primary_zone():
    ctx = _names(0, _stack("mail.old.example", "old.example"), "new.example",
                 zones_status=403, token_zones=("old.example",))
    assert ctx["_ms_host_reachable"] is False


def test_a_restore_converge_sees_the_primary_zone_alone():
    """--skip-tags edge skips the zone listing, so its answer is undefined; the
    names file reads that as no listing and raises no undefined-variable error."""
    ctx = _names(0, _stack("mail.old.example", "old.example"), "new.example",
                 zones_status=None)
    assert ctx["_ms_host_reachable"] is False
    ctx = _names(0, _stack("mail.new.example", "new.example"), "new.example",
                 zones_status=None)
    assert ctx["_ms_host_reachable"] is True


def test_only_the_edge_tagged_zone_listing_calls_the_cloudflare_api():
    """The restore converge skips `edge`: the names file, which the cert task
    file includes outside it, reaches Cloudflare through nothing else."""
    assert "cloudflare_api_base" not in (_ROLE / "tasks" / "_mailserver_names.yml").read_text()
    include = _task(CERT, "the zones the Cloudflare token reaches")
    assert include["tags"] == ["edge"]
    assert include["ansible.builtin.include_tasks"]["apply"]["tags"] == ["edge"]


def test_a_zone_name_must_match_on_a_label_boundary():
    ctx = _names(0, _stack("mail.notnew.example", "notnew.example"), "new.example")
    assert ctx["_ms_host_reachable"] is False


def test_issuance_asks_for_the_stack_hostname_and_only_when_reachable():
    issue = _task(CERT, "issue cert via DNS-01")
    argv = issue["ansible.builtin.command"]["argv"]
    assert "mailserver_stack_hostname" in argv
    assert "mailserver_hostname" not in argv
    assert "_ms_host_reachable | bool" in issue["when"]


def test_the_live_dir_the_hook_env_and_the_policy_id_follow_the_stack_hostname():
    for value in (DEFAULTS["mailserver_cert_live_dir"], DEFAULTS["mailserver_mta_sts_policy_id"]):
        assert "mailserver_stack_hostname" in value
        assert "mailserver_hostname" not in value
    assert "mailserver_hostname" not in ENV_TEMPLATE
    assert "mailserver_stack_hostname" in ENV_TEMPLATE


def test_an_unreachable_certificate_name_is_reported():
    keep = _task(CERT, "keep the existing certificate")
    assert keep["when"] == "not (_ms_host_reachable | bool)"


def _names_in(task) -> str:
    return yaml.dump(task)


def test_every_dns_record_is_named_from_the_stack_and_gated_on_its_zone():
    """The A record sits in the hostname's zone, the two TXT records in the
    mail domain's; none is named from the primary zone."""
    for fragment, reach, zone_id in (
        ("publish the mail A record", "_ms_host_reachable", "_ms_host_zone_id"),
        ("publish MTA-STS + TLS-RPT", "_ms_domain_reachable", "_ms_domain_zone_id"),
    ):
        block = _task(DNS, fragment)
        assert f"{reach} | bool" in block["when"]
        text = _names_in(block["block"])
        assert zone_id in text
        assert "cloudflare_zone" not in text
        assert "mailserver_hostname" not in text


def test_a_name_left_alone_is_reported():
    report = _task(DNS, "report the records left alone")
    assert "_ms_host_reachable" in str(report["when"])
    assert "_ms_domain_reachable" in str(report["when"])
