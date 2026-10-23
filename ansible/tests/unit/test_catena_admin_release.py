"""The registry rules catena-ce/ansible/helpers/fetch_release.py follows when
an install names no release: which tag is the newest catena-admin release,
how the tag list is read, and how an anonymous pull token is taken.

Every test here is offline: the HTTP layer is injected. A test that reached
ghcr.io would pass or fail on the state of the registry rather than on the code.

Run: uv run pytest tests/unit/test_catena_admin_release.py
"""
from __future__ import annotations

import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "helpers"))

import catena_admin_release as car  # noqa: E402


class _Resp(io.BytesIO):
    """Enough of a urllib response for the helper: headers plus a body."""

    def __init__(self, body: bytes = b"", headers: dict | None = None):
        super().__init__(body)
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class _Pages:
    """A registry answering get(path) from a list of (body, headers) pages,
    recording each path asked for."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.asked: list[str] = []

    def get(self, path):
        self.asked.append(path)
        body, headers = self.pages[len(self.asked) - 1]
        return headers, body


def _tags(tags, headers=None):
    return json.dumps({"tags": tags}).encode(), headers or {}


# ─── choosing the version ───────────────────────────────────────────────────

def test_the_newest_release_wins_and_is_ordered_numerically():
    """String ordering puts v0.9.0 above v0.10.0, which would pin a client to
    an older panel every time a minor rolls over."""
    assert car.newest_release(["v0.9.0", "v0.10.0", "v0.2.0"]) == "v0.10.0"
    assert car.newest_release(["v1.0.0", "v0.99.99"]) == "v1.0.0"
    assert car.newest_release(["v0.1.9", "v0.1.10"]) == "v0.1.10"


def test_pre_releases_and_moving_tags_are_not_candidates():
    """`latest` and `sha-abc1234` are published on every release, and an -rc is
    published deliberately. None of them is what a client's first install
    should pull, and excluding them cannot be got wrong the way an ordering
    rule can."""
    assert car.newest_release(
        ["latest", "sha-3da4e79", "v0.5.1", "v0.6.0-rc1",
         "sha256-205a5a70f953225d565c2593584ffe5fcace72bdfd4616279a714c98bb01f7d2"]
    ) == "v0.5.1"
    assert car.newest_release(["latest", "main", "edge"]) == ""


# ─── reading the tag list ───────────────────────────────────────────────────

def test_the_tag_list_is_paginated_to_the_end():
    """Registries page tag lists, and the page break does not fall on version
    order -- a first-page-only read can miss the newest release entirely and
    silently install an older panel."""
    registry = _Pages([
        _tags(["v0.1.0", "latest"], {
            "Link": '</v2/catenahq/catena-admin/tags/list?n=100&last=latest>; rel="next"'}),
        _tags(["v0.9.0"]),
    ])
    tags = car.list_tags(registry, "catenahq/catena-admin")
    assert car.newest_release(tags) == "v0.9.0"
    assert registry.asked == [
        "/v2/catenahq/catena-admin/tags/list?n=100",
        "/v2/catenahq/catena-admin/tags/list?n=100&last=latest",
    ]


def test_a_self_referential_next_link_does_not_loop_forever():
    """A registry that returns its own page as `next` would otherwise hang the
    install rather than fail it."""
    registry = _Pages([_tags(["v0.5.1"], {
        "Link": '</v2/catenahq/catena-admin/tags/list?n=100>; rel="next"'})])
    assert car.list_tags(registry, "catenahq/catena-admin") == ["v0.5.1"]
    assert len(registry.asked) == 1


def test_the_manifest_accept_names_every_media_type():
    """A registry handed an Accept it cannot satisfy may answer with a schema-1
    manifest, whose digest is NOT what the tag resolves to. Pinning that digest
    would fail the payload gate on every host."""
    for media in ("oci.image.index.v1+json", "oci.image.manifest.v1+json",
                  "docker.distribution.manifest.list.v2+json",
                  "docker.distribution.manifest.v2+json"):
        assert media in car._MANIFEST_ACCEPT, f"{media} missing from Accept"


# ─── talking to the registry ────────────────────────────────────────────────

def _unauthorized(header):
    return urllib.error.HTTPError(
        "https://ghcr.io/v2/catenahq/catena-admin/tags/list", 401, "Unauthorized",
        {"WWW-Authenticate": header}, None)


def test_the_token_is_acquired_from_the_challenge_not_hardcoded():
    """The 401 names the realm, service and scope. Reading them is what keeps
    this working against a registry that is not ghcr.io, and against ghcr.io
    changing its token endpoint."""
    challenge = car._bearer_challenge(_unauthorized(
        'Bearer realm="https://ghcr.io/token",service="ghcr.io",'
        'scope="repository:catenahq/catena-admin:pull"'))
    assert challenge == {"realm": "https://ghcr.io/token", "service": "ghcr.io",
                         "scope": "repository:catenahq/catena-admin:pull"}
    asked = []

    def open_it(req, timeout=None):
        asked.append(req.full_url)
        return _Resp(json.dumps({"token": "anon"}).encode())

    assert car._fetch_token(challenge, timeout=1, opener=open_it) == "anon"
    assert asked[0].startswith("https://ghcr.io/token?")
    assert "service=ghcr.io" in asked[0] and "scope=repository" in asked[0]


def test_a_challenge_that_is_not_bearer_is_no_challenge():
    assert car._bearer_challenge(_unauthorized('Basic realm="x"')) is None


def test_a_token_endpoint_that_returns_no_token_fails():
    with pytest.raises(car.ResolveError):
        car._fetch_token({"realm": "https://ghcr.io/token"}, timeout=1,
                         opener=lambda req, timeout=None: _Resp(b"{}"))


def test_the_registry_host_is_split_off_the_repository_path():
    """A first component is a registry only when it looks like a host. Reading
    `catenahq` as one would send every request to a name that does not
    resolve."""
    assert car.split_repository("ghcr.io/catenahq/catena-admin") == (
        "ghcr.io", "catenahq/catena-admin")
    assert car.split_repository("localhost:5000/catena-admin") == (
        "localhost:5000", "catena-admin")
    assert car.split_repository("catenahq/catena-admin") == (
        "registry-1.docker.io", "catenahq/catena-admin")


# ─── failing the right way ──────────────────────────────────────────────────

def test_a_network_blip_is_transient_and_an_http_answer_is_not():
    """A 404 on the repository reads the same on the third attempt as on the
    first. Retrying it turns a clear message into a slow one."""
    assert car._transient(urllib.error.URLError("Temporary failure in name resolution"))
    assert car._transient(TimeoutError())
    assert not car._transient(urllib.error.HTTPError(
        "https://ghcr.io/v2/", 404, "Not Found", {}, None))
