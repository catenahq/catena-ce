"""helpers/fetch_release.py: a server unpacks exactly the image it names, by
digest, before Docker exists.

The registry is a local fixture with the shapes GHCR has: a 401 that names a
token endpoint, an OCI index over two platforms, and blob URLs that redirect
to a storage host on another port which refuses any request carrying an
Authorization header. Nothing here reaches the internet.

Run: uv run pytest tests/unit/test_fetch_release.py
"""
from __future__ import annotations

import functools
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ANSIBLE_DIR = Path(__file__).resolve().parents[2]
if str(ANSIBLE_DIR) not in sys.path:
    sys.path.insert(0, str(ANSIBLE_DIR))

from helpers import fetch_release as fr  # noqa: E402

OCI_INDEX = "application/vnd.oci.image.index.v1+json"
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
GZIP_LAYER = "application/vnd.oci.image.layer.v1.tar+gzip"
ZSTD_LAYER = "application/vnd.oci.image.layer.v1.tar+zstd"
TOKEN = "fixture-token"
REPO_PATH = "catenahq/catena-admin"
CE = "usr/local/share/catena-ce"
EE = "usr/local/share/catena-ee"
GHCR = "ghcr.io/catenahq/catena-admin"
D = {name: "sha256:" + c * 64 for name, c in
     (("image", "1"), ("pin", "2"), ("swarm", "3"), ("newest", "4"))}

CONNECT = functools.partial(fr.Registry, insecure_http=True, delay_s=0)


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _layer(*members) -> bytes:
    """A gzip tar layer. Each member is (kind, name, ...) with kind one of
    dir, file (content, mode), symlink (target), hardlink (target), fifo."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for kind, name, *rest in members:
            info = tarfile.TarInfo(name)
            data = None
            if kind == "dir":
                info.type, info.mode = tarfile.DIRTYPE, 0o755
            elif kind == "file":
                data = rest[0]
                info.size = len(data)
                info.mode = rest[1] if len(rest) > 1 else 0o644
            elif kind == "symlink":
                info.type, info.linkname = tarfile.SYMTYPE, rest[0]
            elif kind == "hardlink":
                info.type, info.linkname = tarfile.LNKTYPE, rest[0]
            elif kind == "fifo":
                info.type = tarfile.FIFOTYPE
            tar.addfile(info, io.BytesIO(data) if data is not None else None)
    return gzip.compress(raw.getvalue())


ROOTS = (("dir", "usr"), ("dir", "usr/local"), ("dir", "usr/local/share"),
         ("dir", CE), ("dir", EE))

LAYER_1 = _layer(
    *ROOTS,
    ("file", f"{CE}/VENDOR.json", b'{"tree_sha256": "abc"}\n'),
    ("dir", f"{CE}/ansible"),
    ("file", f"{CE}/ansible/site.yml", b"first\n"),
    ("file", f"{CE}/ansible/gone.yml", b"whited out\n"),
    ("dir", f"{CE}/ansible/old"),
    ("file", f"{CE}/ansible/old/a.txt", b"from layer one\n"),
    ("symlink", f"{CE}/ansible/current.yml", "site.yml"),
    ("fifo", f"{CE}/ansible/pipe"),
    ("dir", f"{EE}/bin"),
    ("file", f"{EE}/bin/engine", b"amd64 engine\n", 0o4755),
    ("hardlink", f"{EE}/bin/engine-link", f"{EE}/bin/engine"),
    ("dir", "etc"),
    ("file", "etc/passwd", b"root:x:0:0\n"),
    ("file", "usr/local/bin/tool", b"outside\n", 0o755),
)

# The new file sits BEFORE the opaque marker of its own directory: the marker
# reaches only the layers below, whatever the order inside the tar.
LAYER_2 = _layer(
    ("file", f"{CE}/ansible/.wh.gone.yml", b""),
    ("file", f"{CE}/ansible/old/b.txt", b"from layer two\n"),
    ("file", f"{CE}/ansible/old/.wh..wh..opq", b""),
    ("file", f"{CE}/ansible/site.yml", b"second\n"),
    ("file", f"{EE}/README", b"payload\n"),
)

ARM_LAYER = _layer(
    *ROOTS,
    ("file", f"{CE}/VENDOR.json", b"{}\n"),
    ("dir", f"{EE}/bin"),
    ("file", f"{EE}/bin/engine", b"arm64 engine\n", 0o755),
)


def _reply(handler, code, body=b"", headers=None, *, send_body=True):
    handler.send_response(code)
    for key, value in (headers or {}).items():
        handler.send_header(key, value)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    if send_body:
        handler.wfile.write(body)


def _server(serve):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            serve(self, True)

        def do_HEAD(self):
            serve(self, False)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02},
                     daemon=True).start()
    return server


class _Registry:
    """A registry with token auth, and the storage host its blobs redirect to."""

    def __init__(self):
        self.blobs: dict[str, bytes] = {}
        self.manifests: dict[str, tuple[str, bytes]] = {}
        self.tags: list[str] = []
        self.digest_header = True
        self.drop_blob_once = False
        self.storage_auth: list = []
        self.token_requests = 0
        self.registry = _server(self._serve_registry)
        self.storage = _server(self._serve_storage)
        self.host = f"127.0.0.1:{self.registry.server_port}"
        self.repository = f"{self.host}/{REPO_PATH}"

    def close(self):
        for server in (self.registry, self.storage):
            server.shutdown()
            server.server_close()

    def _serve_registry(self, h, send_body):
        url = urllib.parse.urlsplit(h.path)
        if url.path == "/token":
            self.token_requests += 1
            return _reply(h, 200, json.dumps({"token": TOKEN}).encode(),
                          send_body=send_body)
        if h.headers.get("Authorization") != f"Bearer {TOKEN}":
            challenge = (f'Bearer realm="http://{self.host}/token",service="fixture",'
                         f'scope="repository:{REPO_PATH}:pull"')
            return _reply(h, 401, headers={"WWW-Authenticate": challenge},
                          send_body=send_body)
        m = re.fullmatch(rf"/v2/{REPO_PATH}/(manifests|blobs|tags)/(.+)", url.path)
        if m is None:
            return _reply(h, 404, send_body=send_body)
        kind, ref = m.groups()
        if kind == "tags":
            return _reply(h, 200, json.dumps({"tags": self.tags}).encode(),
                          send_body=send_body)
        if kind == "manifests":
            if ref not in self.manifests:
                return _reply(h, 404, send_body=send_body)
            media, data = self.manifests[ref]
            headers = {"Content-Type": media}
            if self.digest_header:
                headers["Docker-Content-Digest"] = _digest(data)
            return _reply(h, 200, data, headers, send_body=send_body)
        if ref not in self.blobs:
            return _reply(h, 404, send_body=send_body)
        signed = f"http://127.0.0.1:{self.storage.server_port}/signed/{ref}?sig=x"
        return _reply(h, 307, headers={"Location": signed}, send_body=send_body)

    def _serve_storage(self, h, send_body):
        self.storage_auth.append(h.headers.get("Authorization"))
        if h.headers.get("Authorization"):
            return _reply(h, 400, b"Only one auth mechanism allowed",
                          send_body=send_body)
        if self.drop_blob_once:
            self.drop_blob_once = False
            return None  # closes the connection with no response at all
        digest = urllib.parse.urlsplit(h.path).path.rsplit("/", 1)[1]
        return _reply(h, 200, self.blobs[digest], send_body=send_body)

    def _blob(self, data: bytes) -> str:
        self.blobs[_digest(data)] = data
        return _digest(data)

    def _manifest(self, media: str, data: bytes) -> str:
        self.manifests[_digest(data)] = (media, data)
        return _digest(data)

    def publish(self, platforms: dict, *, tag: str = "v1.2.3",
                media: str = GZIP_LAYER) -> str:
        """An OCI index over one image per platform; returns repo:tag@digest."""
        entries = []
        for platform, layers in platforms.items():
            os_name, arch = platform.split("/")
            config = json.dumps({"os": os_name, "architecture": arch}).encode()
            manifest = json.dumps({
                "schemaVersion": 2, "mediaType": OCI_MANIFEST,
                "config": {"mediaType": "application/vnd.oci.image.config.v1+json",
                           "digest": self._blob(config), "size": len(config)},
                "layers": [{"mediaType": media, "digest": self._blob(layer),
                            "size": len(layer)} for layer in layers],
            }).encode()
            entries.append({"mediaType": OCI_MANIFEST,
                            "digest": self._manifest(OCI_MANIFEST, manifest),
                            "size": len(manifest),
                            "platform": {"os": os_name, "architecture": arch}})
        index = json.dumps({"schemaVersion": 2, "mediaType": OCI_INDEX,
                            "manifests": entries}).encode()
        digest = self._manifest(OCI_INDEX, index)
        self.manifests[tag] = (OCI_INDEX, index)
        self.tags.append(tag)
        return f"{self.repository}:{tag}@{digest}"


@pytest.fixture
def registry(monkeypatch):
    for var in ("http_proxy", "https_proxy", "all_proxy",
                "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.delenv(var, raising=False)
    fixture = _Registry()
    yield fixture
    fixture.close()


@pytest.fixture
def image(registry):
    return registry.publish({"linux/amd64": [LAYER_1, LAYER_2],
                             "linux/arm64": [ARM_LAYER]})


def _fetch(ref, dest, **kw):
    kw.setdefault("platform", "linux/amd64")
    fr.fetch(ref, dest, connect=CONNECT, **kw)


# --- reading a reference ----------------------------------------------------

def test_a_registry_port_is_part_of_the_repository_not_a_tag():
    ref = fr.parse_ref("127.0.0.1:5000/catenahq/catena-admin:bench")
    assert ref == fr.Ref("127.0.0.1:5000/catenahq/catena-admin", "127.0.0.1:5000",
                         "catenahq/catena-admin", "bench", "")
    bare = fr.parse_ref("127.0.0.1:5000/catenahq/catena-admin")
    assert (bare.repository, bare.tag, bare.digest) == (
        "127.0.0.1:5000/catenahq/catena-admin", "", "")


def test_a_reference_carries_a_tag_a_digest_or_both():
    digest = D["image"]
    tag_only = fr.parse_ref(f"{GHCR}:v0.5.1")
    assert (tag_only.host, tag_only.path, tag_only.tag, tag_only.digest) == (
        "ghcr.io", REPO_PATH, "v0.5.1", "")
    digest_only = fr.parse_ref(f"{GHCR}@{digest}")
    assert (digest_only.tag, digest_only.digest) == ("", digest)
    both = fr.parse_ref(f"127.0.0.1:5000/{REPO_PATH}:bench@{digest}")
    assert (both.host, both.tag, both.digest) == ("127.0.0.1:5000", "bench", digest)
    for ref in (f"{GHCR}:v0.5.1", f"{GHCR}@{digest}",
                f"127.0.0.1:5000/{REPO_PATH}:bench@{digest}"):
        assert fr.parse_ref(ref).name == ref


@pytest.mark.parametrize("ref", [
    "", f"{GHCR}@sha256:abc", f"{GHCR}:v1?x=1", f"{GHCR}@md5:" + "0" * 32,
])
def test_a_malformed_reference_is_refused(ref):
    with pytest.raises(fr.FetchError):
        fr.parse_ref(ref)


# --- choosing the image -----------------------------------------------------

@pytest.fixture
def sources(tmp_path, monkeypatch):
    """Fakes for the running service and the newest release that record being
    asked, and a store that pins GHCR at v0.5.1."""
    state = {"asked": [], "swarm": f"{GHCR}:v0.4.0@{D['swarm']}",
             "config": tmp_path / "config.json"}
    state["config"].write_text(json.dumps(
        {"image_pins": {GHCR: f"{GHCR}:v0.5.1@{D['pin']}"}}))

    def swarm(repository):
        state["asked"].append("swarm")
        return state["swarm"]

    def newest(registry, repository):
        state["asked"].append("newest")
        return f"{repository}:v0.9.0@{D['newest']}"

    monkeypatch.setattr(fr, "swarm_image", swarm)
    monkeypatch.setattr(fr, "latest_release", newest)
    return state


def _resolve(sources, **kw):
    return fr.resolve_ref(GHCR, config=str(sources["config"]), **kw)


def test_an_explicit_image_wins_over_everything(sources):
    ref = f"127.0.0.1:5000/{REPO_PATH}:bench@{D['image']}"
    assert _resolve(sources, image=ref) == ref
    assert sources["asked"] == []


def test_an_explicit_release_wins_over_the_pin_and_gets_its_digest(
        registry, image, tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    config.write_text(json.dumps(
        {"image_pins": {registry.repository: f"{registry.repository}:v0.0.1"}}))
    monkeypatch.setattr(fr, "swarm_image", lambda repository: pytest.fail("asked"))
    assert fr.resolve_ref(registry.repository, release="v1.2.3",
                          config=str(config), connect=CONNECT) == image


def test_a_tag_without_a_digest_header_is_hashed_from_its_manifest(
        registry, image, tmp_path):
    registry.digest_header = False
    assert fr.resolve_ref(registry.repository, release="v1.2.3",
                          config=str(tmp_path / "none.json"),
                          connect=CONNECT) == image


def test_the_host_pin_wins_over_the_service_and_the_newest_release(sources):
    assert _resolve(sources) == f"{GHCR}:v0.5.1@{D['pin']}"
    assert sources["asked"] == []


def test_a_pin_for_another_repository_or_a_non_release_tag_is_not_used(sources):
    sources["config"].write_text(json.dumps({"image_pins": {
        GHCR: f"{GHCR}:latest@{D['pin']}",
        "ghcr.io/catenahq/other": f"ghcr.io/catenahq/other:v9.9.9@{D['pin']}",
    }}))
    assert _resolve(sources) == sources["swarm"]


def test_the_running_service_answers_when_nothing_is_pinned(sources):
    sources["config"].unlink()
    assert _resolve(sources) == sources["swarm"]
    assert sources["asked"] == ["swarm"]


def test_the_newest_release_is_the_last_resort(sources):
    sources["config"].unlink()
    sources["swarm"] = ""
    assert _resolve(sources) == f"{GHCR}:v0.9.0@{D['newest']}"
    assert sources["asked"] == ["swarm", "newest"]


def test_the_newest_release_is_the_highest_vxyz_tag_on_the_registry(
        registry, image, tmp_path, monkeypatch):
    newer = registry.publish({"linux/amd64": [LAYER_1]}, tag="v1.10.0")
    registry.publish({"linux/amd64": [LAYER_1]}, tag="v2.0.0-rc1")
    registry.publish({"linux/amd64": [LAYER_1]}, tag="latest")
    monkeypatch.setattr(fr, "swarm_image", lambda repository: "")
    assert fr.resolve_ref(registry.repository, config=str(tmp_path / "none.json"),
                          connect=CONNECT) == newer


def _fake_docker(tmp_path, monkeypatch, prints, code=0):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(f'#!/bin/sh\necho "$@" > "{tmp_path}/argv"\n'
                      f"echo '{prints}'\nexit {code}\n")
    docker.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))


def test_the_service_image_is_read_from_the_swarm_spec(tmp_path, monkeypatch):
    running = f"{GHCR}:v0.5.1@{D['swarm']}"
    _fake_docker(tmp_path, monkeypatch, running)
    assert fr.swarm_image(GHCR) == running
    assert (tmp_path / "argv").read_text().split() == [
        "service", "inspect", "catena-admin",
        "--format", "{{.Spec.TaskTemplate.ContainerSpec.Image}}"]


def test_a_service_image_from_another_repository_is_not_used(tmp_path, monkeypatch):
    _fake_docker(tmp_path, monkeypatch, f"127.0.0.1:5000/{REPO_PATH}:bench")
    assert fr.swarm_image(GHCR) == ""


def test_no_service_or_no_docker_names_nothing(tmp_path, monkeypatch):
    _fake_docker(tmp_path, monkeypatch, "", code=1)
    assert fr.swarm_image(GHCR) == ""
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert fr.swarm_image(GHCR) == ""


# --- pulling and unpacking --------------------------------------------------

def test_every_layer_applies_with_its_whiteouts(registry, image, tmp_path):
    dest = tmp_path / "dest"
    _fetch(image, dest)
    ansible = dest / CE / "ansible"
    assert (ansible / "site.yml").read_bytes() == b"second\n"
    assert not (ansible / "gone.yml").exists()
    assert sorted(os.listdir(ansible / "old")) == ["b.txt"]
    assert os.readlink(ansible / "current.yml") == "site.yml"
    assert not os.path.lexists(ansible / "pipe")
    assert (dest / EE / "README").read_bytes() == b"payload\n"
    assert (dest / EE / "bin/engine-link").read_bytes() == b"amd64 engine\n"


def test_only_the_requested_paths_are_written(registry, image, tmp_path):
    dest = tmp_path / "dest"
    _fetch(image, dest)
    assert sorted(os.listdir(dest)) == ["IMAGE", "usr"]
    assert sorted(os.listdir(dest / "usr/local")) == ["share"]
    assert sorted(os.listdir(dest / "usr/local/share")) == ["catena-ce", "catena-ee"]

    only_ce = tmp_path / "only-ce"
    _fetch(image, only_ce, paths=[CE])
    assert sorted(os.listdir(only_ce / "usr/local/share")) == ["catena-ce"]


def test_modes_are_kept_without_setuid_and_dest_is_private(registry, image, tmp_path):
    dest = tmp_path / "dest"
    _fetch(image, dest)
    assert stat.S_IMODE((dest / EE / "bin/engine").stat().st_mode) == 0o755
    assert stat.S_IMODE((dest / CE / "ansible/site.yml").stat().st_mode) == 0o644
    assert stat.S_IMODE(dest.stat().st_mode) == 0o700


def test_image_records_the_ref_the_tree_came_from(registry, image, tmp_path):
    dest = tmp_path / "dest"
    _fetch(image, dest)
    assert (dest / "IMAGE").read_text() == image + "\n"


def test_the_platform_picks_its_own_build(registry, image, tmp_path):
    dest = tmp_path / "dest"
    _fetch(image, dest, platform="linux/arm64")
    assert (dest / EE / "bin/engine").read_bytes() == b"arm64 engine\n"
    with pytest.raises(fr.FetchError, match="linux/amd64"):
        _fetch(image, tmp_path / "s390x", platform="linux/s390x")


def test_blob_redirects_reach_storage_without_the_registry_token(
        registry, image, tmp_path):
    _fetch(image, tmp_path / "dest")
    assert registry.token_requests >= 1
    assert registry.storage_auth and set(registry.storage_auth) == {None}


def test_a_dropped_connection_is_retried(registry, image, tmp_path):
    registry.drop_blob_once = True
    _fetch(image, tmp_path / "dest")
    assert (tmp_path / "dest" / CE / "ansible/site.yml").read_bytes() == b"second\n"


def _tampered(data: bytes) -> bytes:
    return bytes([data[0] ^ 1]) + data[1:]


def test_a_blob_that_does_not_hash_to_its_digest_is_refused_and_nothing_lands(
        registry, image, tmp_path):
    registry.blobs[_digest(LAYER_2)] = _tampered(LAYER_2)
    dest = tmp_path / "dest"
    with pytest.raises(fr.FetchError, match=_digest(LAYER_2)):
        _fetch(image, dest)
    assert os.listdir(dest) == []


def test_a_failed_fetch_leaves_the_previous_tree_whole(registry, image, tmp_path):
    dest = tmp_path / "dest"
    _fetch(image, dest)
    registry.blobs[_digest(LAYER_2)] = _tampered(LAYER_2)
    with pytest.raises(fr.FetchError):
        _fetch(image, dest)
    assert (dest / CE / "ansible/site.yml").read_bytes() == b"second\n"
    assert (dest / "IMAGE").read_text() == image + "\n"
    assert sorted(os.listdir(dest)) == ["IMAGE", "usr"]


def test_a_manifest_that_does_not_hash_to_the_requested_digest_is_refused(
        registry, image, tmp_path):
    forged = "sha256:" + "f" * 64
    registry.manifests[forged] = registry.manifests["v1.2.3"]
    with pytest.raises(fr.FetchError, match=forged):
        _fetch(f"{registry.repository}:v1.2.3@{forged}", tmp_path / "dest")


def test_a_platform_manifest_that_does_not_hash_to_its_index_entry_is_refused(
        registry, image, tmp_path):
    index = json.loads(registry.manifests["v1.2.3"][1])
    amd64 = next(e["digest"] for e in index["manifests"]
                 if e["platform"]["architecture"] == "amd64")
    registry.manifests[amd64] = (OCI_MANIFEST, b'{"layers": []}')
    with pytest.raises(fr.FetchError, match=amd64):
        _fetch(image, tmp_path / "dest")


def test_a_zstd_layer_is_refused_by_its_media_type(registry, tmp_path):
    ref = registry.publish({"linux/amd64": [b"never read"]}, tag="zstd",
                           media=ZSTD_LAYER)
    with pytest.raises(fr.FetchError, match=re.escape(ZSTD_LAYER)):
        _fetch(ref, tmp_path / "dest")
    assert registry.storage_auth == []


@pytest.mark.parametrize("layers, reason", [
    ([_layer(*ROOTS, ("file", f"{CE}/../../../../../evil", b"x"))], "climbs"),
    ([_layer(*ROOTS, ("file", "../evil", b"x"))], "climbs"),
    ([_layer(*ROOTS, ("file", f"/{CE}/evil", b"x"))], "absolute"),
    ([_layer(*ROOTS, ("symlink", f"{CE}/etc", "/etc"))], "outside the image"),
    ([_layer(*ROOTS, ("symlink", f"{CE}/up", "../../../../../etc"))],
     "outside the image"),
    # Each link alone resolves inside; followed the way the kernel does,
    # x climbs past the root.
    ([_layer(*ROOTS, ("symlink", f"{CE}/top", "../../.."),
             ("symlink", f"{CE}/x", "top/../../../.."))], "outside the image"),
    ([_layer(*ROOTS, ("symlink", f"{CE}/ln", "../catena-ee")),
      _layer(("file", f"{CE}/ln/planted", b"x"))], "symlink a layer placed"),
], ids=["dotdot-inside", "dotdot", "absolute", "absolute-link", "escaping-link",
        "link-chain", "write-through-link"])
def test_a_hostile_member_is_refused_and_nothing_lands(
        registry, tmp_path, layers, reason):
    ref = registry.publish({"linux/amd64": layers}, tag="hostile")
    dest = tmp_path / "dest"
    with pytest.raises(fr.FetchError, match=reason):
        _fetch(ref, dest)
    assert os.listdir(dest) == []
    assert not (tmp_path / "evil").exists()


# --- the command line -------------------------------------------------------

def test_resolve_only_prints_the_ref_and_fetches_nothing(tmp_path, capsys):
    ref = f"{GHCR}:v0.5.1@{D['image']}"
    assert fr.main(["--image", ref, "--resolve-only",
                    "--config", str(tmp_path / "none.json")]) == 0
    assert capsys.readouterr().out == ref + "\n"
    assert os.listdir(tmp_path) == []


def test_the_script_runs_from_its_files_sent_flat(tmp_path):
    """The installer sends the helper and what it loads into one directory on
    a server with no catena-ce tree yet."""
    for src in (ANSIBLE_DIR / "helpers" / "fetch_release.py",
                ANSIBLE_DIR / "helpers" / "catena_admin_release.py",
                ANSIBLE_DIR / "playbooks" / "filter_plugins" / "image_pin.py"):
        shutil.copy(src, tmp_path / src.name)
    ref = f"{GHCR}:v0.5.1@{D['image']}"
    done = subprocess.run(
        [sys.executable, "-I", str(tmp_path / "fetch_release.py"), "--image", ref,
         "--resolve-only", "--config", str(tmp_path / "none.json")],
        capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    assert done.stdout == ref + "\n"


def test_the_cli_fetches_and_keeps_stdout_for_the_ref(registry, image, tmp_path,
                                                     capsys):
    dest = tmp_path / "dest"
    assert fr.main(["--repository", registry.repository, "--release", "v1.2.3",
                    "--dest", str(dest), "--platform", "linux/amd64",
                    "--insecure-http", "--config", str(tmp_path / "none.json")]) == 0
    out = capsys.readouterr()
    assert out.out == image + "\n"
    assert "layer 2/2" in out.err
    assert (dest / "IMAGE").read_text() == image + "\n"


def test_the_cli_fails_with_one_line_naming_the_reason(registry, tmp_path, capsys):
    ref = registry.publish({"linux/amd64": [b"never read"]}, tag="zstd",
                           media=ZSTD_LAYER)
    assert fr.main(["--repository", registry.repository, "--image", ref,
                    "--dest", str(tmp_path / "dest"), "--platform", "linux/amd64",
                    "--insecure-http"]) == 1
    out = capsys.readouterr()
    assert out.out == ""
    assert ZSTD_LAYER in out.err.strip().splitlines()[-1]
