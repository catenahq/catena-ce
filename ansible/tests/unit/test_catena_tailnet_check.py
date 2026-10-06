"""Unit tests for scripts/catena-tailnet-check.py -- the check a Settings save
runs on tailnet credentials before the panel stores them. The HTTP layer is a
scripted fake, so nothing reaches Tailscale or a Headscale server."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ANSIBLE_DIR = Path(__file__).resolve().parents[2]
MOD_PATH = ANSIBLE_DIR / "scripts" / "catena-tailnet-check.py"


@pytest.fixture(scope="module")
def tc():
    spec = importlib.util.spec_from_file_location("catena_tailnet_check", MOD_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Http:
    """Answers by (method, path suffix); records every call."""

    def __init__(self, answers: dict[tuple[str, str], tuple[int, object]]):
        self.answers = answers
        self.calls: list[tuple[str, str, dict, bytes | None]] = []

    def __call__(self, method, url, *, headers=None, data=None):
        self.calls.append((method, url, headers or {}, data))
        for (m, suffix), answer in self.answers.items():
            if m == method and url.endswith(suffix):
                return answer
        return 404, None


TAILSCALE = {
    "TAILNET_PROVIDER": "tailscale",
    "TAILSCALE_TAGS": "tag:vps, tag:web",
    "tailscale_oauth_client_id": "cid",
    "tailscale_oauth_client_secret": "csecret",
}

TS_OK = {
    ("POST", "/oauth/token"): (200, {"access_token": "tok"}),
    ("POST", "/tailnet/-/keys"): (200, {"id": "k1"}),
    ("GET", "/tailnet/-/devices"): (200, {"devices": []}),
}


def test_working_tailscale_credentials_pass(tc):
    http = _Http(TS_OK)
    assert tc.check(TAILSCALE, http) == {"valid": True, "reason": ""}
    # The throwaway key carries every tag the join advertises, and dies in a
    # minute on its own.
    _, _, _, body = next(c for c in http.calls if c[1].endswith("/keys"))
    key = json.loads(body)
    assert key["capabilities"]["devices"]["create"]["tags"] == ["tag:vps", "tag:web"]
    assert key["expirySeconds"] == 60


@pytest.mark.parametrize("failing, words", [
    (("POST", "/oauth/token"), "refused the OAuth client"),
    (("POST", "/tailnet/-/keys"), "cannot create keys tagged tag:vps, tag:web"),
    (("GET", "/tailnet/-/devices"), "cannot read devices"),
])
def test_each_tailscale_failure_names_its_cause(tc, failing, words):
    answers = dict(TS_OK)
    answers[failing] = (403, None)
    verdict = tc.check(TAILSCALE, _Http(answers))
    assert not verdict["valid"]
    assert words in verdict["reason"]


@pytest.mark.parametrize("blank", [
    "tailscale_oauth_client_id", "tailscale_oauth_client_secret", "TAILSCALE_TAGS",
])
def test_a_missing_tailscale_value_is_refused_without_asking(tc, blank):
    http = _Http(TS_OK)
    verdict = tc.check({**TAILSCALE, blank: ""}, http)
    assert not verdict["valid"]
    assert http.calls == []


def test_the_reason_never_carries_a_credential(tc):
    answers = dict(TS_OK)
    answers[("POST", "/oauth/token")] = (401, None)
    reason = tc.check(TAILSCALE, _Http(answers))["reason"]
    assert "csecret" not in reason and "cid" not in reason


HEADSCALE = {
    "TAILNET_PROVIDER": "headscale",
    "TAILNET_CONTROL_URL": "https://hs.example.net/",
    "HEADSCALE_USER": "servers",
    "headscale_api_key": "hskey",
}

# GET /version as Headscale answers it, with no credential.
HS_VERSION = {("GET", "/version"): (200, {"version": "v0.29.4", "commit": "c"})}


def test_a_headscale_api_key_must_list_the_named_user(tc):
    listing = {**HS_VERSION,
               ("GET", "/api/v1/user"): (200, {"users": [{"name": "servers"}]})}
    assert tc.check(HEADSCALE, _Http(listing))["valid"]
    other = {**HS_VERSION,
             ("GET", "/api/v1/user"): (200, {"users": [{"name": "people"}]})}
    verdict = tc.check(HEADSCALE, _Http(other))
    assert not verdict["valid"] and "no user named servers" in verdict["reason"]


def test_a_refused_headscale_api_key_is_reported(tc):
    verdict = tc.check(HEADSCALE, _Http({**HS_VERSION,
                                         ("GET", "/api/v1/user"): (401, None)}))
    assert not verdict["valid"] and "refused the API key" in verdict["reason"]


def test_a_headscale_preauth_key_alone_needs_the_server_to_answer(tc):
    req = {**HEADSCALE, "headscale_api_key": "", "headscale_preauth_key": "pk"}
    assert tc.check(req, _Http(HS_VERSION))["valid"]
    verdict = tc.check(req, _Http({("GET", "/version"): (0, None)}))
    assert not verdict["valid"] and "does not answer" in verdict["reason"]


@pytest.mark.parametrize("version", ["v0.29.0", "0.29.4", "v0.29.0-beta.4", "v0.30.1", "v1.0.0"])
def test_a_headscale_at_or_above_the_minimum_passes(tc, version):
    req = {**HEADSCALE, "headscale_api_key": "", "headscale_preauth_key": "pk"}
    answers = {("GET", "/version"): (200, {"version": version})}
    assert tc.check(req, _Http(answers)) == {"valid": True, "reason": ""}


@pytest.mark.parametrize("answer, words", [
    ((200, {"version": "v0.28.0"}), "runs v0.28.0; Headscale 0.29 or later is required"),
    ((200, {"version": "0.26.1"}), "runs 0.26.1"),
    # Headscale serves /version from 0.27 on; a server without it is older.
    ((404, None), "does not report its version (GET /version answered HTTP 404)"),
    ((200, {"version": "dev"}), "reports version 'dev', which is not a release number"),
    ((200, None), "does not report its version"),
])
def test_a_headscale_below_the_minimum_or_unreadable_is_refused(tc, answer, words):
    http = _Http({("GET", "/version"): answer,
                  ("GET", "/api/v1/user"): (200, {"users": [{"name": "servers"}]})})
    verdict = tc.check(HEADSCALE, http)
    assert not verdict["valid"] and words in verdict["reason"]
    assert "0.29 or later is required" in verdict["reason"]
    # Refused on the version alone: the API key is never sent.
    assert [c[1] for c in http.calls] == ["https://hs.example.net/version"]
    assert http.calls[0][2] == {}


@pytest.mark.parametrize("url", ["http://10.0.0.250:8080", "https://hs.example.net"])
def test_both_schemes_are_accepted(tc, url):
    req = {**HEADSCALE, "TAILNET_CONTROL_URL": url,
           "headscale_api_key": "", "headscale_preauth_key": "pk"}
    assert tc.check(req, _Http(HS_VERSION))["valid"]


@pytest.mark.parametrize("change, words", [
    ({"TAILNET_CONTROL_URL": "hs.example.net"}, "must start with http:// or https://"),
    ({"TAILNET_CONTROL_URL": "ftp://hs.example.net"}, "must start with http:// or https://"),
    ({"headscale_api_key": ""}, "API key or pre-authentication key is required"),
    ({"HEADSCALE_USER": ""}, "user is required"),
])
def test_incomplete_headscale_settings_are_refused(tc, change, words):
    http = _Http(HS_VERSION)
    verdict = tc.check({**HEADSCALE, **change}, http)
    assert not verdict["valid"] and words in verdict["reason"]
    assert http.calls == []


def test_an_unknown_provider_is_refused(tc):
    assert not tc.check({"TAILNET_PROVIDER": "zerotier"}, _Http({}))["valid"]
