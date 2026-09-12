#!/usr/bin/env python3
"""Ask the registry which catena-admin release to install, and for its digest.

WHAT THIS REPLACES. The image was two hand-maintained literals in
bootstrap/roles/common/defaults -- `catena_admin_image_floor` and
`catena_admin_image_floor_digest` -- bumped together by hand on every
catena-admin release. Drift between them is the one thing that pair cannot
survive, because reconcile/roles/payload refuses to extract from an image whose digest is
not the recorded one:

    ghcr.io/catenahq/catena-admin:v0.5.1 resolved to sha256:205a5a70... but
    this deployment pins sha256:09e03d74... Refusing to extract or
    root-execute the payload.

A correct host, a published image, and a fresh install that cannot proceed. The
two literals answered ONE question -- "which bytes is this release of catena-ce
installing" -- and a question with two writers is answered wrong eventually.
There is now one writer, and it is the registry.

THE HIGHEST RELEASE TAG, NOT `:latest`. Both name the newest publish; only one
of them is checkable.

  - `:latest` is moved by `docker buildx imagetools create`, which builds a NEW
    index. Its digest is therefore NOT the digest that was scanned, signed and
    written to the release's image-digest.txt -- today `:latest` is
    sha256:51399efb... while the v0.5.1 it points at is sha256:205a5a70...
    Recording the index digest would pin a byte string no release artifact
    mentions.
  - A version tag is what publish-image.yml pushes, scans, signs and attests.
    It is also immutable in practice: the workflow only ever pushes a tag once.

So this resolves `vX.Y.Z` tags and takes the highest. Pre-releases are excluded
outright rather than ordered below releases: a `-rc1` publish must not become
what a client's first install pulls, and "excluded" cannot be got wrong the way
an ordering rule can.

WHAT THIS DOES NOT DO. It does not verify a signature. The digest it returns is
whatever the registry says the tag resolves to right now, so the assertion
downstream ("the image I am about to extract is the one I resolved") is a
same-converge consistency check, not provenance. Provenance is `cosign verify`,
which needs cosign on the host; see the pin note in bootstrap/roles/common/defaults.

Emits one JSON object on stdout:

    {"repository": "ghcr.io/catenahq/catena-admin",
     "version":    "v0.5.1",
     "tag_ref":    "ghcr.io/catenahq/catena-admin:v0.5.1",
     "digest":     "sha256:205a5a70...",
     "ref":        "ghcr.io/catenahq/catena-admin:v0.5.1@sha256:205a5a70..."}

Runs on the VPS, from playbooks/tasks/load_onbox_config.yml -- the machine that
resolves the tag is then the machine that pulls it. Self-contained on purpose:
ansible.builtin.script copies ONE file to the host, so it may not import
net_retry. The caller retries instead (`until` on the task).

Unit tests: ../tests/unit/test_catena_admin_release.py
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_REPOSITORY = "ghcr.io/catenahq/catena-admin"
DEFAULT_TIMEOUT_S = 20.0

# A published release, v-prefix optional. No pre-release and no build suffix:
# see the module docstring for why those are excluded rather than ordered.
_RELEASE_TAG = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")

# Every manifest media type the registry might answer with. Sending all four
# matters: a registry handed an Accept it cannot satisfy may answer with a
# schema-1 manifest whose digest is NOT the one the tag resolves to.
_MANIFEST_ACCEPT = ", ".join((
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
))

_BEARER_PARAM = re.compile(r'(\w+)="([^"]*)"')


class ResolveError(RuntimeError):
    """Anything that leaves the caller without a version AND a digest."""


def split_repository(repository: str) -> tuple[str, str]:
    """(registry host, repository path) for an image repository reference.

    The first component is a registry only when it looks like a host -- it has
    a dot or a port. `catenahq/catena-admin` is a Docker Hub path, not a
    registry called `catenahq`."""
    repository = (repository or "").strip().strip("/")
    if not repository:
        raise ResolveError("no repository given")
    head, _, rest = repository.partition("/")
    if rest and ("." in head or ":" in head or head == "localhost"):
        return head, rest
    return "registry-1.docker.io", repository


def _read(url: str, *, token: str, timeout: float, method: str = "GET",
          accept: str = "application/json", opener=None):
    """One registry request. Returns (headers, body-bytes)."""
    if opener is None:  # resolved at call time so a test's patch is honoured
        opener = urllib.request.urlopen
    req = urllib.request.Request(url, method=method)
    req.add_header("Accept", accept)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with opener(req, timeout=timeout) as resp:  # noqa: S310
        # A HEAD has no body; .read() on one is empty rather than an error.
        return dict(resp.headers), resp.read()


def _bearer_challenge(exc: urllib.error.HTTPError) -> dict[str, str] | None:
    """The realm/service/scope a 401 is asking for; None for a non-bearer
    challenge."""
    if exc.code != 401:
        return None
    header = exc.headers.get("WWW-Authenticate", "") if exc.headers else ""
    if not header.lower().startswith("bearer "):
        return None
    return dict(_BEARER_PARAM.findall(header))


def _fetch_token(challenge: dict[str, str], *, timeout: float,
                 opener=None) -> str:
    """Trade a bearer challenge for an anonymous pull token.

    The image is public, so no credential is presented. A registry that
    requires one answers 401 here too, and the caller reports that rather than
    silently falling back to an unauthenticated read that returns nothing."""
    realm = challenge.get("realm")
    if not realm:
        raise ResolveError("registry asked for a bearer token but named no realm")
    params = {k: v for k, v in challenge.items()
              if k in ("service", "scope") and v}
    url = realm + ("&" if "?" in realm else "?") + urllib.parse.urlencode(params)
    _, body = _read(url, token="", timeout=timeout, opener=opener)
    doc = json.loads(body.decode("utf-8"))
    token = doc.get("token") or doc.get("access_token") or ""
    if not token:
        raise ResolveError(f"{realm} returned no token")
    return str(token)


class _Registry:
    """A registry read that acquires its token on the first 401 and reuses it.

    Acquiring on the challenge rather than up front is what keeps this working
    against a registry that serves public reads unauthenticated."""

    def __init__(self, host: str, *, timeout: float, opener=None):
        self.host = host
        self.timeout = timeout
        self.opener = opener
        self.token = ""

    def get(self, path: str, *, method: str = "GET", accept: str = "application/json"):
        url = f"https://{self.host}{path}"
        try:
            return _read(url, token=self.token, timeout=self.timeout,
                         method=method, accept=accept, opener=self.opener)
        except urllib.error.HTTPError as exc:
            challenge = _bearer_challenge(exc)
            if challenge is None or self.token:
                raise
            self.token = _fetch_token(challenge, timeout=self.timeout,
                                      opener=self.opener)
            return _read(url, token=self.token, timeout=self.timeout,
                         method=method, accept=accept, opener=self.opener)


def _next_page(headers: dict[str, str]) -> str:
    """The path in a `Link: <...>; rel="next"` header, or "".

    Registries page tag lists. Reading only the first page silently caps the
    answer, and the cap lands on the OLDEST tags -- the list is not ordered by
    version, so a truncated read can miss the newest release entirely."""
    link = headers.get("Link") or headers.get("link") or ""
    for part in link.split(","):
        if 'rel="next"' not in part.replace(" ", ""):
            continue
        start, end = part.find("<"), part.find(">")
        if 0 <= start < end:
            return part[start + 1:end]
    return ""


def list_tags(registry: _Registry, path: str) -> list[str]:
    page = f"/v2/{path}/tags/list?n=100"
    tags: list[str] = []
    seen: set[str] = set()
    while page and page not in seen:
        seen.add(page)
        headers, body = registry.get(page)
        doc = json.loads(body.decode("utf-8"))
        tags.extend(str(t) for t in (doc.get("tags") or []))
        page = _next_page(headers)
    return tags


def newest_release(tags) -> str:
    """The highest vX.Y.Z tag, or "" when there is none."""
    best, best_key = "", None
    for tag in tags:
        m = _RELEASE_TAG.match(str(tag).strip())
        if not m:
            continue
        key = tuple(int(g) for g in m.groups())
        if best_key is None or key > best_key:
            best, best_key = str(tag).strip(), key
    return best


def resolve_digest(registry: _Registry, path: str, tag: str) -> str:
    headers, _ = registry.get(f"/v2/{path}/manifests/{tag}", method="HEAD",
                              accept=_MANIFEST_ACCEPT)
    digest = (headers.get("Docker-Content-Digest")
              or headers.get("docker-content-digest") or "").strip()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ResolveError(
            f"{path}:{tag} did not resolve to a sha256 digest (got {digest!r}); "
            "without one the payload extract would run unchecked")
    return digest


def resolve(repository: str = DEFAULT_REPOSITORY, *,
            timeout: float = DEFAULT_TIMEOUT_S, opener=None) -> dict[str, str]:
    host, path = split_repository(repository)
    registry = _Registry(host, timeout=timeout, opener=opener)
    tags = list_tags(registry, path)
    version = newest_release(tags)
    if not version:
        raise ResolveError(
            f"{repository} publishes no vX.Y.Z tag. Found: "
            f"{', '.join(sorted(tags)) or '(none)'}")
    digest = resolve_digest(registry, path, version)
    tag_ref = f"{repository}:{version}"
    return {
        "repository": repository,
        "version": version,
        "tag_ref": tag_ref,
        "digest": digest,
        "ref": f"{tag_ref}@{digest}",
    }


def _transient(exc: BaseException) -> bool:
    """True for a blip worth waiting out, false for an answer.

    An HTTPError IS an answer -- a 404 on the repository will read the same on
    the third attempt as on the first -- so it is not retried. HTTPError
    subclasses URLError, so it has to be tested first."""
    if isinstance(exc, urllib.error.HTTPError):
        return False
    return isinstance(exc, (urllib.error.URLError, TimeoutError,
                            ConnectionError, OSError))


def resolve_with_retry(repository: str, *, timeout: float,
                       attempts: int = 3, delay_s: float = 5.0,
                       sleep=None, opener=None) -> dict[str, str]:
    """resolve(), waiting out transient network errors.

    The retry lives here rather than on the Ansible task because this runs
    inside the converge's config loader: a name-resolution blip at that one
    moment would otherwise fail a whole install, which is the fragility this
    module was written to remove rather than relocate."""
    if sleep is None:
        import time
        sleep = time.sleep
    for attempt in range(1, attempts + 1):
        try:
            return resolve(repository, timeout=timeout, opener=opener)
        except Exception as exc:  # noqa: BLE001 - re-raised below
            if attempt >= attempts or not _transient(exc):
                raise
            print(f"catena-admin-release: {exc}; attempt {attempt}/{attempts}, "
                  f"retrying in {delay_s:.0f}s", file=sys.stderr)
            sleep(delay_s)
    raise AssertionError("resolve_with_retry: unreachable")  # pragma: no cover


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repository", default=DEFAULT_REPOSITORY)
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    ap.add_argument("--attempts", type=int, default=3)
    args = ap.parse_args(argv)
    try:
        print(json.dumps(resolve_with_retry(
            args.repository, timeout=args.timeout, attempts=args.attempts)))
    except (ResolveError, urllib.error.URLError, OSError,
            ValueError) as exc:
        print(f"catena-admin-release: {args.repository}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
