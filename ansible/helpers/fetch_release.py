#!/usr/bin/env python3
"""Pull a catena-admin release image from its registry and unpack the catena-ce tree and the payload out of it.

Runs on a fresh Debian 12 or 13 server before Docker exists: the installer
copies it over SSH and runs it with the system python3, so it is stdlib only
and keeps to what Python 3.11.2 has (no tarfile extraction filters, no zstd).
It loads catena_admin_release.py and image_pin.py from beside it, or in a
catena-ce tree image_pin.py from ../playbooks/filter_plugins/, so a caller
that sends it on its own sends those two files with it.

WHICH IMAGE. The first of these that names one:

  1. --image, a full reference;
  2. --release, a tag on --repository;
  3. the host's pin, image_pins[--repository] in /etc/catena/config.json,
     matched by image_pin.catena_image_pinned;
  4. the image the catena-admin swarm service runs, when it is from
     --repository;
  5. the newest vX.Y.Z tag on --repository, chosen by catena_admin_release.

A reference without a digest is resolved to the one its tag names now. The
result, `repo:tag@sha256:...`, is what gets pulled, what stdout carries, and
what <dest>/IMAGE records.

WHAT IT CHECKS. Every manifest and blob is hashed against the digest that
names it, starting from the resolved one, so the bytes unpacked are exactly
the image named. Layers apply in order with their whiteouts, keeping only the
--path trees, into a staging directory inside --dest; each tree replaces its
previous copy only once every layer is in. Members are vetted here because
3.11.2 has no extraction filters: no absolute or `..` names, no link that
resolves outside the image root, no write through a symlink a layer placed,
no device or fifo, no setuid or setgid bit, and no owner (everything belongs
to whoever runs this; --dest is created 0700).

Usage:
  fetch_release.py --repository ghcr.io/catenahq/catena-admin --dest DIR
                   [--image REF | --release vX.Y.Z] [--config PATH]
                   [--path P ...] [--platform linux/amd64] [--ca-file F]
                   [--insecure-http] [--resolve-only]

Unit tests: ../tests/unit/test_fetch_release.py
"""
from __future__ import annotations

import argparse
import functools
import hashlib
import http.client
import importlib.util
import json
import os
import re
import shutil
import ssl
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import NamedTuple

_HERE = Path(__file__).resolve().parent


def _load(name: str, *candidates: Path):
    path = next((p for p in candidates if p.is_file()), candidates[0])
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Loaded by path, so the same lines work for the files sent flat to a server
# and for helpers.fetch_release imported from a catena-ce tree.
car = _load("catena_admin_release", _HERE / "catena_admin_release.py")
image_pin = _load("image_pin", _HERE / "image_pin.py",
                  _HERE.parent / "playbooks" / "filter_plugins" / "image_pin.py")

DEFAULT_CONFIG = "/etc/catena/config.json"
DEFAULT_PATHS = ("usr/local/share/catena-ce", "usr/local/share/catena-ee")
SERVICE = "catena-admin"

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_TAG = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
_HOST = re.compile(r"^[A-Za-z0-9.-]+(?::[0-9]+)?$")
_PATH = re.compile(
    r"^[a-z0-9]+(?:[._-]+[a-z0-9]+)*(?:/[a-z0-9]+(?:[._-]+[a-z0-9]+)*)*$")
_ARCH = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}

# The layer encodings tarfile reads on 3.11.2. A zstd layer needs a
# decompressor that python3 on Debian 12 lacks, so it is refused by name.
_LAYER_MODES = {
    "application/vnd.oci.image.layer.v1.tar+gzip": "r|gz",
    "application/vnd.docker.image.rootfs.diff.tar.gzip": "r|gz",
    "application/vnd.oci.image.layer.v1.tar": "r|",
}
_WHITEOUT = ".wh."
_OPAQUE = ".wh..wh..opq"
_REDIRECTS = (301, 302, 303, 307, 308)
_MAX_REDIRECTS = 5
_MAX_HOPS = 40  # the kernel's own limit on symlinks in one path walk
_CHUNK = 1 << 20


class FetchError(RuntimeError):
    """Anything that leaves --dest without the requested image."""


def _say(message: str) -> None:
    print(f"fetch-release: {message}", file=sys.stderr, flush=True)


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


# --- naming the image -------------------------------------------------------

class Ref(NamedTuple):
    repository: str
    host: str
    path: str
    tag: str
    digest: str

    @property
    def name(self) -> str:
        return (self.repository + (f":{self.tag}" if self.tag else "")
                + (f"@{self.digest}" if self.digest else ""))


def parse_ref(ref: str) -> Ref:
    """registry/repo[:tag][@sha256:<hex>] split into its parts.

    The tag split is image_pin's, where a colon followed by a slash is a
    registry port; the host split is catena_admin_release's."""
    ref = (ref or "").strip()
    name, _, digest = ref.partition("@")
    if not name:
        raise FetchError("empty image reference")
    if digest and not _DIGEST.fullmatch(digest):
        raise FetchError(f"{ref}: the digest is not sha256:<64 hex>")
    repository, tag = image_pin._split_ref(name)
    host, path = car.split_repository(repository)
    if not (_HOST.fullmatch(host) and _PATH.fullmatch(path)
            and (not tag or _TAG.fullmatch(tag))):
        raise FetchError(f"{ref} is not an image reference")
    return Ref(repository, host, path, tag, digest)


def pinned_image(repository: str, config: str = DEFAULT_CONFIG) -> str:
    """The pin this host recorded for repository, or "".

    An absent store is a fresh host and pins nothing; a malformed one is an
    error. That is onbox_config.image_pins' posture, which is not importable
    here: it loads the knob registry at import."""
    store = Path(config)
    if not store.exists():
        return ""
    doc = json.loads(store.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise FetchError(f"{store}: top level is not an object")
    return image_pin.catena_image_pinned(doc.get("image_pins"), repository)


def swarm_image(repository: str) -> str:
    """The image the catena-admin service runs, when it is from repository.

    An image from another repository answers for that repository, and this
    fetch reads --repository."""
    docker = shutil.which("docker")
    if not docker:
        return ""
    done = subprocess.run(
        [docker, "service", "inspect", SERVICE,
         "--format", "{{.Spec.TaskTemplate.ContainerSpec.Image}}"],
        capture_output=True, text=True, timeout=60, check=False)
    image = done.stdout.strip() if done.returncode == 0 else ""
    return image if image_pin._split_ref(image)[0] == repository else ""


def latest_release(registry: Registry, repository: str) -> str:
    """repository:<its highest vX.Y.Z tag>, by catena_admin_release's rule."""
    tags = car.list_tags(registry, car.split_repository(repository)[1])
    version = car.newest_release(tags)
    if not version:
        raise FetchError(f"{repository} publishes no vX.Y.Z tag")
    return f"{repository}:{version}"


def manifest_digest(registry: Registry, repo_path: str, tag: str) -> str:
    """The digest tag names now: the registry's Docker-Content-Digest, else
    the sha256 of the manifest it serves."""
    url = f"/v2/{repo_path}/manifests/{tag}"
    headers, _ = registry.get(url, method="HEAD", accept=car._MANIFEST_ACCEPT)
    digest = (headers.get("Docker-Content-Digest") or "").strip()
    if _DIGEST.fullmatch(digest):
        return digest
    _, body = registry.get(url, accept=car._MANIFEST_ACCEPT)
    return _sha256(body)


def resolve_ref(repository: str, *, image: str = "", release: str = "",
                config: str = DEFAULT_CONFIG, connect=None) -> str:
    """The image to fetch, as repo:tag@sha256:<digest of the top manifest>.

    Each source is asked only when every one before it named nothing; see
    the module docstring for the order."""
    connect = connect or Registry
    sources = (
        ("--image", lambda: image),
        ("--release", lambda: f"{repository}:{release}" if release else ""),
        (f"image_pins in {config}", lambda: pinned_image(repository, config)),
        (f"the {SERVICE} service", lambda: swarm_image(repository)),
        ("the newest release", lambda: latest_release(
            connect(car.split_repository(repository)[0]), repository)),
    )
    for source, find in sources:
        ref = find()
        if ref:
            break
    parsed = parse_ref(ref)
    if not parsed.digest:
        if not parsed.tag:
            raise FetchError(f"{ref} names neither a tag nor a digest")
        parsed = parsed._replace(digest=manifest_digest(
            connect(parsed.host), parsed.path, parsed.tag))
    _say(f"{parsed.name} (from {source})")
    return parsed.name


# --- talking to the registry ------------------------------------------------

def _transient(exc: BaseException) -> bool:
    """catena_admin_release's rule, plus what a pull meets that a tag lookup
    does not: a body cut short, and a gateway error from a registry's front."""
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in (502, 503, 504)
    return isinstance(exc, http.client.IncompleteRead) or car._transient(exc)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Registry:
    """Registry API v2 reads against one host, with an anonymous bearer token
    taken on the first 401 and replaced on a later one."""

    def __init__(self, host: str, *, insecure_http: bool = False,
                 ca_file: str | None = None,
                 timeout: float = car.DEFAULT_TIMEOUT_S,
                 attempts: int = 3, delay_s: float = 5.0):
        self.host = host
        self.insecure_http = insecure_http
        self.base = f"{'http' if insecure_http else 'https'}://{host}"
        self.timeout = timeout
        self.attempts = attempts
        self.delay_s = delay_s
        self.token = ""
        context = ssl.create_default_context()
        if ca_file:
            context.load_verify_locations(cafile=ca_file)
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=context), _NoRedirect())

    def _send(self, req: urllib.request.Request, timeout: float | None = None):
        """Open req, following redirects here so a hop to another host goes
        without the Authorization header: GHCR sends blobs to a signed storage
        URL that refuses a second credential."""
        for _ in range(_MAX_REDIRECTS + 1):
            scheme = urllib.parse.urlsplit(req.full_url).scheme
            if scheme != "https" and not (scheme == "http" and self.insecure_http):
                raise FetchError(f"refusing {req.full_url}: only https is "
                                 "allowed without --insecure-http")
            try:
                return self._opener.open(req, timeout=timeout or self.timeout)
            except urllib.error.HTTPError as exc:
                location = exc.headers.get("Location")
                if exc.code not in _REDIRECTS or not location:
                    raise
                exc.close()
            url = urllib.parse.urljoin(req.full_url, location)
            # req.headers leaves out Host, which urllib sets per request.
            headers = dict(req.headers)
            here = urllib.parse.urlsplit(req.full_url).netloc
            if urllib.parse.urlsplit(url).netloc != here:
                headers.pop("Authorization", None)
            req = urllib.request.Request(url, headers=headers, method=req.get_method())
        raise FetchError(f"more than {_MAX_REDIRECTS} redirects from {self.host}")

    def _request(self, url: str, method: str, accept: str) -> urllib.request.Request:
        headers = {"Accept": accept}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return urllib.request.Request(url, headers=headers, method=method)

    def _open(self, path: str, *, method: str = "GET", accept: str = "*/*"):
        url = self.base + path
        try:
            return self._send(self._request(url, method, accept))
        except urllib.error.HTTPError as exc:
            challenge = car._bearer_challenge(exc)
            if challenge is None:
                raise
            exc.close()
        # A 401 with a token in hand is one that expired during a long pull.
        self.token = car._fetch_token(challenge, timeout=self.timeout,
                                      opener=self._send)
        return self._send(self._request(url, method, accept))

    def _retry(self, once):
        attempt = 1
        while True:
            try:
                return once()
            except Exception as exc:  # noqa: BLE001 - re-raised unless transient
                if attempt >= self.attempts or not _transient(exc):
                    raise
                _say(f"{exc}; attempt {attempt}/{self.attempts}, "
                     f"retrying in {self.delay_s:.0f}s")
                time.sleep(self.delay_s)
                attempt += 1

    def get(self, path: str, *, method: str = "GET",
            accept: str = "application/json"):
        """(headers, body) of one read: the shape catena_admin_release's
        list_tags expects of a registry."""
        def once():
            with self._open(path, method=method, accept=accept) as resp:
                return resp.headers, resp.read()
        return self._retry(once)

    def download(self, repo_path: str, layer: dict, target: Path) -> None:
        """Stream a blob into target, refusing it unless it hashes to its
        digest."""
        digest, size = layer["digest"], layer.get("size")

        def once():
            sha, got = hashlib.sha256(), 0
            with self._open(f"/v2/{repo_path}/blobs/{digest}") as resp, \
                    open(target, "wb") as out:
                while chunk := resp.read(_CHUNK):
                    sha.update(chunk)
                    out.write(chunk)
                    got += len(chunk)
            if isinstance(size, int) and got < size:
                # http.client ends a cut-off body quietly. A short body is a
                # dropped connection, worth another attempt; a wrong hash is not.
                raise http.client.IncompleteRead(b"", size - got)
            return "sha256:" + sha.hexdigest()

        actual = self._retry(once)
        if actual != digest:
            raise FetchError(f"blob {digest} hashes to {actual}; refusing it")


def _manifest(registry: Registry, repo_path: str, digest: str, what: str) -> dict:
    _, body = registry.get(f"/v2/{repo_path}/manifests/{digest}",
                           accept=car._MANIFEST_ACCEPT)
    actual = _sha256(body)
    if actual != digest:
        raise FetchError(f"the {what} {digest} hashes to {actual}; refusing it")
    doc = json.loads(body)
    if not isinstance(doc, dict):
        raise FetchError(f"the {what} {digest} is not a JSON object")
    return doc


def _platform_entry(entries, platform: str) -> dict:
    want = platform.split("/")
    if len(want) not in (2, 3):
        raise FetchError(f"--platform {platform!r} is not os/arch[/variant]")
    os_name, arch, variant = (want + [""])[:3]
    offered = []
    for entry in entries if isinstance(entries, list) else ():
        p = entry.get("platform") if isinstance(entry, dict) else None
        if not isinstance(p, dict):
            continue
        offered.append(f"{p.get('os')}/{p.get('architecture')}")
        if (p.get("os") == os_name and p.get("architecture") == arch
                and (not variant or p.get("variant") == variant)):
            if not _DIGEST.fullmatch(str(entry.get("digest"))):
                raise FetchError(f"the {platform} entry names no sha256 digest")
            return entry
    raise FetchError(f"the image has no {platform} build "
                     f"(it has {', '.join(offered) or 'none'})")


def image_layers(registry: Registry, repo_path: str, digest: str,
                 platform: str) -> list:
    """The layer descriptors of the platform's image under digest, each
    manifest on the way checked against the digest that names it."""
    doc = _manifest(registry, repo_path, digest, "image")
    if "manifests" in doc:
        entry = _platform_entry(doc["manifests"], platform)
        doc = _manifest(registry, repo_path, entry["digest"], f"{platform} manifest")
    layers = doc.get("layers")
    if not isinstance(layers, list):
        raise FetchError(f"{digest} is neither an image index nor an image "
                         f"manifest (mediaType {doc.get('mediaType')!r})")
    for layer in layers:
        if not isinstance(layer, dict) or not _DIGEST.fullmatch(str(layer.get("digest"))):
            raise FetchError(f"a layer of {digest} names no sha256 digest")
        if layer.get("mediaType") not in _LAYER_MODES:
            raise FetchError(
                f"layer {layer['digest']} is {layer.get('mediaType')}; only gzip "
                "and uncompressed tar layers can be read here")
    return layers


def host_platform() -> str:
    machine = os.uname().machine
    if machine not in _ARCH:
        raise FetchError(f"no catena-admin build for a {machine} machine; "
                         "pass --platform")
    return f"linux/{_ARCH[machine]}"


# --- unpacking --------------------------------------------------------------

def _member_path(name: str) -> str:
    if name.startswith("/"):
        raise FetchError(f"layer member {name!r} is an absolute path; refusing the image")
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if ".." in parts:
        raise FetchError(f"layer member {name!r} climbs with '..'; refusing the image")
    return "/".join(parts)


def _prefixes(paths) -> list[str]:
    out = []
    for raw in paths:
        parts = [p for p in raw.split("/") if p not in ("", ".")]
        if not parts or ".." in parts:
            raise FetchError(f"--path {raw!r} names no directory inside the image")
        out.append("/".join(parts))
    out = list(dict.fromkeys(out))
    # A path inside another requested one arrives with it.
    return [p for p in out if not any(p != q and _under(p, q) for q in out)]


def _under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def _join(parent: str, name: str) -> str:
    return f"{parent}/{name}" if parent else name


def _real_dir(root: Path, rel: str, *, create: bool) -> Path | None:
    """root/rel reached through real directories only.

    With create, missing directories are made, and a file in the way is
    replaced, which is what a later layer's path over it means; a symlink in
    the way is refused, since writing through it would land wherever it
    points. Without create, None for anything but a real directory."""
    current = root
    for part in rel.split("/") if rel else ():
        current = current / part
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            if not create:
                return None
            os.mkdir(current, 0o755)
            continue
        if stat.S_ISDIR(mode):
            continue
        if not create:
            return None
        if stat.S_ISLNK(mode):
            raise FetchError(f"{rel} goes through {current.relative_to(root)}, a "
                             "symlink a layer placed; refusing to write through it")
        os.unlink(current)
        os.mkdir(current, 0o755)
    return current


def _clear(target: Path) -> None:
    try:
        mode = os.lstat(target).st_mode
    except FileNotFoundError:
        return
    if stat.S_ISDIR(mode):
        shutil.rmtree(target)
    else:
        os.unlink(target)


def _remove(tree: Path, rel: str) -> None:
    parent, _, leaf = rel.rpartition("/")
    directory = _real_dir(tree, parent, create=False)
    if directory is not None:
        _clear(directory / leaf)


def _empty(tree: Path, rel: str) -> None:
    directory = _real_dir(tree, rel, create=False)
    if directory is not None:
        for child in os.listdir(directory):
            _clear(directory / child)


def _write(tar: tarfile.TarFile, member: tarfile.TarInfo, path: str,
           tree: Path, prefixes: list[str], dir_modes: dict) -> None:
    if not (member.isdir() or member.isreg() or member.issym() or member.islnk()):
        return  # devices and fifos
    parent, _, leaf = path.rpartition("/")
    target = _real_dir(tree, parent, create=True) / leaf
    mode = member.mode & 0o7777 & ~(stat.S_ISUID | stat.S_ISGID)
    if member.isdir():
        if _real_dir(tree, path, create=False) is None:
            _clear(target)
            os.mkdir(target, 0o755)
        # Applied once every layer is in, so a read-only directory still
        # takes the files a later layer puts in it.
        dir_modes[path] = mode
        return
    _clear(target)
    if member.isreg():
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as out:
            shutil.copyfileobj(tar.extractfile(member), out, _CHUNK)
            os.fchmod(out.fileno(), mode)
    elif member.issym():
        os.symlink(member.linkname, target)
    else:
        source = _member_path(member.linkname)
        if not any(_under(source, p) for p in prefixes):
            raise FetchError(f"{path} is a hard link to {source}, outside the "
                             "requested paths")
        src_parent, _, src_leaf = source.rpartition("/")
        src_dir = _real_dir(tree, src_parent, create=False)
        if src_dir is None:
            raise FetchError(f"{path} is a hard link to {source}, which is not there")
        os.link(src_dir / src_leaf, target, follow_symlinks=False)


def apply_layer(blob: Path, mode: str, tree: Path, prefixes: list[str],
                dir_modes: dict) -> None:
    """Apply one layer to tree: its whiteouts first, against what the layers
    below it left, then its own members under the requested prefixes.

    Two passes because a layer may list an opaque marker after a sibling it
    adds itself, and the marker reaches only the layers below. Only the
    prefixes are ever in tree, so a whiteout elsewhere removes nothing, and
    one over an ancestor of a prefix removes that prefix."""
    with open(blob, "rb") as raw, tarfile.open(fileobj=raw, mode=mode) as tar:
        for member in tar:
            parent, _, leaf = _member_path(member.name).rpartition("/")
            if leaf == _OPAQUE:
                _empty(tree, parent)
            elif (leaf.startswith(_WHITEOUT) and not leaf.startswith(_WHITEOUT * 2)
                    and len(leaf) > len(_WHITEOUT)):
                _remove(tree, _join(parent, leaf[len(_WHITEOUT):]))
    with open(blob, "rb") as raw, tarfile.open(fileobj=raw, mode=mode) as tar:
        for member in tar:
            path = _member_path(member.name)
            if (path and not path.rpartition("/")[2].startswith(_WHITEOUT)
                    and any(_under(path, p) for p in prefixes)):
                _write(tar, member, path, tree, prefixes, dir_modes)


def _resolves_inside(tree: Path, rel: str) -> bool:
    """Whether the symlink at tree/rel stays inside tree, reading tree as /.

    Links met on the way are expanded where they sit, the way the kernel
    walks a path, so a chain of links that each look inside cannot climb out
    together. The answer is the same wherever tree is moved to."""
    where = rel.split("/")[:-1]
    target = os.readlink(tree / rel)
    todo: list[str] = []
    for _ in range(_MAX_HOPS):
        if target.startswith("/"):
            return False
        todo = target.split("/") + todo
        while todo:
            part = todo.pop(0)
            if part in ("", "."):
                continue
            if part == "..":
                if not where:
                    return False
                where.pop()
                continue
            here = tree.joinpath(*where, part)
            if os.path.islink(here):
                target = os.readlink(here)
                break
            where.append(part)
        else:
            return True
    return True  # a loop resolves to nothing, so it reaches nothing outside


def _check_links(tree: Path, prefix: str) -> None:
    for dirpath, dirnames, filenames in os.walk(tree / prefix):
        for name in dirnames + filenames:
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                rel = os.path.relpath(full, tree)
                if not _resolves_inside(tree, rel):
                    raise FetchError(f"{rel} links to {os.readlink(full)}, outside "
                                     "the image; refusing it")


def _install(tree: Path, dest: Path, staging: Path, prefixes: list[str],
             dir_modes: dict) -> None:
    """Move each finished prefix from tree into dest, once all are vetted."""
    for prefix in prefixes:
        if _real_dir(tree, prefix, create=False) is None:
            raise FetchError(f"the image has no directory {prefix}")
    for prefix in prefixes:
        _check_links(tree, prefix)
    for path in sorted(dir_modes, reverse=True):
        directory = _real_dir(tree, path, create=False)
        if directory is not None:
            os.chmod(directory, dir_modes[path])
    for n, prefix in enumerate(prefixes):
        parent, _, leaf = prefix.rpartition("/")
        target = _real_dir(dest, parent, create=True) / leaf
        if os.path.lexists(target):
            os.rename(target, staging / f"previous-{n}")
        os.rename(tree / prefix, target)


def fetch(ref: str, dest, *, paths=DEFAULT_PATHS, platform: str = "",
          connect=None) -> None:
    """Unpack the paths of the image ref names, by digest, into dest, and
    record ref in dest/IMAGE."""
    image = parse_ref(ref)
    if not image.digest:
        raise FetchError(f"{ref} names no digest; resolve it first")
    prefixes = _prefixes(paths)
    platform = platform or host_platform()
    registry = (connect or Registry)(image.host)
    _say(f"fetching {image.name} for {platform}")
    layers = image_layers(registry, image.path, image.digest, platform)
    dest = Path(dest)
    dest.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Inside dest, so each finished tree moves into place with one rename.
    staging = Path(tempfile.mkdtemp(prefix=".fetch-", dir=dest))
    try:
        tree, blob, dir_modes = staging / "tree", staging / "blob", {}
        tree.mkdir()
        for n, layer in enumerate(layers, 1):
            registry.download(image.path, layer, blob)
            apply_layer(blob, _LAYER_MODES[layer["mediaType"]], tree, prefixes,
                        dir_modes)
            blob.unlink()
            _say(f"layer {n}/{len(layers)} {layer['digest']} done")
        _install(tree, dest, staging, prefixes, dir_modes)
        (staging / "IMAGE").write_text(image.name + "\n", encoding="utf-8")
        os.replace(staging / "IMAGE", dest / "IMAGE")
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    _say(f"{', '.join(prefixes)} unpacked into {dest}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("--repository", default=car.DEFAULT_REPOSITORY)
    which = ap.add_mutually_exclusive_group()
    which.add_argument("--image", default="", help="a full image reference")
    which.add_argument("--release", default="", help="a tag on --repository")
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--dest")
    ap.add_argument("--path", action="append", dest="paths",
                    help=f"a directory in the image (default: {' '.join(DEFAULT_PATHS)})")
    ap.add_argument("--platform", default="", help="os/arch (default: this machine)")
    ap.add_argument("--ca-file", help="a CA to trust beside the system ones")
    ap.add_argument("--insecure-http", action="store_true",
                    help="talk plain http to the registry")
    ap.add_argument("--timeout", type=float, default=car.DEFAULT_TIMEOUT_S)
    ap.add_argument("--resolve-only", action="store_true",
                    help="print the resolved reference and fetch nothing")
    args = ap.parse_args(argv)
    if not args.resolve_only and not args.dest:
        ap.error("--dest is required unless --resolve-only")
    connect = functools.partial(Registry, insecure_http=args.insecure_http,
                                ca_file=args.ca_file, timeout=args.timeout)
    try:
        ref = resolve_ref(args.repository, image=args.image, release=args.release,
                          config=args.config, connect=connect)
        if not args.resolve_only:
            fetch(ref, args.dest, paths=args.paths or DEFAULT_PATHS,
                  platform=args.platform, connect=connect)
    except urllib.error.HTTPError as exc:
        print(f"fetch-release: {exc.filename}: HTTP {exc.code} {exc.reason}",
              file=sys.stderr)
        return 1
    except (FetchError, car.ResolveError, urllib.error.URLError, OSError,
            ValueError, EOFError, tarfile.TarError, http.client.HTTPException,
            subprocess.SubprocessError) as exc:
        print(f"fetch-release: {exc}", file=sys.stderr)
        return 1
    print(ref)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
