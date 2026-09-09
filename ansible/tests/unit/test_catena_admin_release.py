"""The registry, not a literal, decides which catena-admin release installs.

The defect this replaces was two hand-maintained values in
bootstrap/roles/common/defaults -- the version and its digest -- that had to be bumped in
lockstep and were not. reconcile/roles/payload refuses to extract from an image whose
digest is not the recorded one, so the drift failed fresh installs on a correct
host holding a correctly published image:

    ...:v0.5.1 resolved to sha256:205a5a70... but this deployment pins
    sha256:09e03d74... Refusing to extract or root-execute the payload.

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


def _opener(routes, *, calls=None):
    """A urlopen stand-in. routes maps a URL substring to a response or an
    exception to raise."""
    def open_it(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if calls is not None:
            calls.append((getattr(req, "method", "GET"), url,
                          dict(getattr(req, "headers", {}))))
        for needle, answer in routes.items():
            if needle in url:
                if isinstance(answer, Exception):
                    raise answer
                return answer
        raise AssertionError(f"no route for {url}")
    return open_it


def _unauthorized(scope="repository:catenahq/catena-admin:pull"):
    return urllib.error.HTTPError(
        "https://ghcr.io/v2/catenahq/catena-admin/tags/list", 401, "Unauthorized",
        {"WWW-Authenticate":
            f'Bearer realm="https://ghcr.io/token",service="ghcr.io",scope="{scope}"'},
        None)


DIGEST = "sha256:" + "20" * 32


def _ghcr(tags, *, digest=DIGEST, tag_headers=None, calls=None):
    return _opener({
        "/token": _Resp(json.dumps({"token": "anon"}).encode()),
        "/tags/list": _Resp(json.dumps({"tags": tags}).encode(),
                            tag_headers or {}),
        "/manifests/": _Resp(b"", {"Docker-Content-Digest": digest}),
    }, calls=calls)


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


def test_a_repository_with_no_release_names_what_it_did_find():
    """A registry answering only `latest` is a real state -- a repo whose
    release workflow has never run -- and "no tags" would send somebody looking
    at the network instead of at the workflow."""
    with pytest.raises(car.ResolveError) as exc:
        car.resolve("ghcr.io/catenahq/catena-admin",
                    opener=_ghcr(["latest", "sha-3da4e79"]))
    assert "latest" in str(exc.value)


# ─── talking to the registry ────────────────────────────────────────────────

def test_the_token_is_acquired_from_the_challenge_not_hardcoded():
    """The 401 names the realm, service and scope. Reading them is what keeps
    this working against a registry that is not ghcr.io, and against ghcr.io
    changing its token endpoint."""
    calls = []
    routes = {
        "/token": _Resp(json.dumps({"token": "anon"}).encode()),
        "/tags/list": _unauthorized(),
        "/manifests/": _Resp(b"", {"Docker-Content-Digest": DIGEST}),
    }
    # First tags/list 401s; after the token is fetched the retry must succeed.
    served = {"n": 0}

    def open_it(req, timeout=None):
        url = req.full_url
        calls.append((req.method, url, dict(req.headers)))
        if "/tags/list" in url:
            served["n"] += 1
            if served["n"] == 1:
                raise routes["/tags/list"]
            return _Resp(json.dumps({"tags": ["v0.5.1"]}).encode())
        for needle, answer in routes.items():
            if needle in url and needle != "/tags/list":
                return answer
        raise AssertionError(url)

    out = car.resolve("ghcr.io/catenahq/catena-admin", opener=open_it)
    assert out["version"] == "v0.5.1"
    token_urls = [u for _, u, _ in calls if "/token" in u]
    assert token_urls and "service=ghcr.io" in token_urls[0]
    assert "scope=repository" in token_urls[0]
    # Everything after the challenge carries the token.
    after = [h for _, u, h in calls if "/tags/list" in u][-1]
    assert after.get("Authorization") == "Bearer anon"


def test_the_tag_list_is_paginated_to_the_end():
    """Registries page tag lists, and the page break does not fall on version
    order -- a first-page-only read can miss the newest release entirely and
    silently install an older panel."""
    pages = {
        1: (json.dumps({"tags": ["v0.1.0", "latest"]}).encode(),
            {"Link": '</v2/catenahq/catena-admin/tags/list?n=100&last=latest>; rel="next"'}),
        2: (json.dumps({"tags": ["v0.9.0"]}).encode(), {}),
    }
    seen = {"n": 0}

    def open_it(req, timeout=None):
        url = req.full_url
        if "/tags/list" in url:
            seen["n"] += 1
            body, headers = pages[seen["n"]]
            return _Resp(body, headers)
        if "/manifests/" in url:
            assert "v0.9.0" in url, "resolved the digest for the wrong tag"
            return _Resp(b"", {"Docker-Content-Digest": DIGEST})
        return _Resp(json.dumps({"token": "anon"}).encode())

    out = car.resolve("ghcr.io/catenahq/catena-admin", opener=open_it)
    assert out["version"] == "v0.9.0"
    assert seen["n"] == 2


def test_a_self_referential_next_link_does_not_loop_forever():
    """A registry that returns its own page as `next` would otherwise hang the
    converge rather than fail it."""
    headers = {"Link": '</v2/catenahq/catena-admin/tags/list?n=100>; rel="next"'}
    out = car.resolve("ghcr.io/catenahq/catena-admin",
                      opener=_ghcr(["v0.5.1"], tag_headers=headers))
    assert out["version"] == "v0.5.1"


def test_the_manifest_request_accepts_every_media_type():
    """A registry handed an Accept it cannot satisfy may answer with a schema-1
    manifest, whose digest is NOT what the tag resolves to. Pinning that digest
    would fail the payload gate on every host."""
    calls = []
    car.resolve("ghcr.io/catenahq/catena-admin", opener=_ghcr(["v0.5.1"], calls=calls))
    method, url, headers = [c for c in calls if "/manifests/" in c[1]][0]
    assert method == "HEAD", "a GET here downloads the manifest for no reason"
    accept = headers.get("Accept", "")
    for media in ("oci.image.index.v1+json", "oci.image.manifest.v1+json",
                  "docker.distribution.manifest.list.v2+json",
                  "docker.distribution.manifest.v2+json"):
        assert media in accept, f"{media} missing from Accept"


def test_a_missing_digest_header_fails_rather_than_pinning_nothing():
    """An empty digest silently disables the payload gate downstream, and an
    unchecked root-executed extract reads exactly like a passing one."""
    opener = _opener({
        "/token": _Resp(json.dumps({"token": "anon"}).encode()),
        "/tags/list": _Resp(json.dumps({"tags": ["v0.5.1"]}).encode()),
        "/manifests/": _Resp(b"", {}),
    })
    with pytest.raises(car.ResolveError) as exc:
        car.resolve("ghcr.io/catenahq/catena-admin", opener=opener)
    assert "digest" in str(exc.value)


# ─── the shape the converge consumes ────────────────────────────────────────

def test_the_ref_is_digest_pinned_so_the_pull_is_byte_addressed():
    """reconcile/roles/payload resolves this at role 5.5 and reconcile/roles/catena-admin creates
    the service at role 13. A tag moved in between must not change which bytes
    land, and the digest in the ref is what makes that impossible."""
    out = car.resolve("ghcr.io/catenahq/catena-admin", opener=_ghcr(["v0.5.1"]))
    assert out == {
        "repository": "ghcr.io/catenahq/catena-admin",
        "version": "v0.5.1",
        "tag_ref": "ghcr.io/catenahq/catena-admin:v0.5.1",
        "digest": DIGEST,
        "ref": f"ghcr.io/catenahq/catena-admin:v0.5.1@{DIGEST}",
    }


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

def test_a_transient_network_error_is_waited_out():
    """This runs inside the converge's config loader. A name-resolution blip at
    that one moment would otherwise fail a whole install -- relocating the
    fragility rather than removing it."""
    attempts = {"n": 0}
    good = _ghcr(["v0.5.1"])

    def flaky(req, timeout=None):
        if "/tags/list" in req.full_url:
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise urllib.error.URLError("Temporary failure in name resolution")
        return good(req, timeout=timeout)

    slept = []
    out = car.resolve_with_retry("ghcr.io/catenahq/catena-admin", timeout=1,
                                 attempts=3, delay_s=5, sleep=slept.append,
                                 opener=flaky)
    assert out["version"] == "v0.5.1"
    assert slept == [5, 5]


def test_an_http_answer_is_not_retried():
    """A 404 on the repository reads the same on the third attempt as on the
    first. Retrying it turns a clear message into a slow one."""
    attempts = {"n": 0}

    def missing(req, timeout=None):
        attempts["n"] += 1
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)

    with pytest.raises(urllib.error.HTTPError):
        car.resolve_with_retry("ghcr.io/catenahq/nope", timeout=1, attempts=3,
                               delay_s=0, sleep=lambda _: None, opener=missing)
    assert attempts["n"] == 1


def test_the_cli_reports_the_reason_on_stderr_and_exits_nonzero(capsys, monkeypatch):
    """The loader puts this string in front of the operator. "failed" is not a
    reason; the registry's own words are."""
    def boom(*a, **kw):
        raise car.ResolveError("ghcr.io said no")
    monkeypatch.setattr(car, "resolve_with_retry", boom)
    assert car.main(["--repository", "ghcr.io/catenahq/catena-admin"]) == 1
    err = capsys.readouterr().err
    assert "ghcr.io said no" in err
    assert "ghcr.io/catenahq/catena-admin" in err
