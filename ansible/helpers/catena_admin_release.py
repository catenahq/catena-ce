"""Registry rules for picking the newest catena-admin release.

catena-ce/ansible/helpers/fetch_release.py loads this from beside it, on a
server before the release is there and in a catena-ce tree: it reads the tag
list here (every page of it), takes the highest release tag by the rule here,
and trades a registry's bearer challenge for an anonymous pull token here.
Self-contained on purpose: the installer sends it flat beside the fetcher, so
it imports nothing from beside it.

THE HIGHEST RELEASE TAG. catena-admin's publish workflow points `:latest` at
every publish, a pre-release included, and `:latest` names no version a host
can record. A version tag names one release, and the workflow pushes each
version once. So the highest `vX.Y.Z` tag wins. Pre-releases are excluded
outright rather than ordered below releases: a `-rc1` publish must not become
what a client's first install pulls, and "excluded" cannot be got wrong the way
an ordering rule can.

Unit tests: ../tests/unit/test_catena_admin_release.py
"""
from __future__ import annotations

import json
import re
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
    """A repository or registry answer the caller cannot use."""


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


def list_tags(registry, path: str) -> list[str]:
    """Every tag of path, across the pages the registry splits them into.

    registry is anything with get(path) -> (headers, body), as
    fetch_release.Registry is."""
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


def _transient(exc: BaseException) -> bool:
    """True for a blip worth waiting out, false for an answer.

    An HTTPError IS an answer -- a 404 on the repository will read the same on
    the third attempt as on the first -- so it is not retried. HTTPError
    subclasses URLError, so it has to be tested first."""
    if isinstance(exc, urllib.error.HTTPError):
        return False
    return isinstance(exc, (urllib.error.URLError, TimeoutError,
                            ConnectionError, OSError))
