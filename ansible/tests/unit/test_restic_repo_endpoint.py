"""Unit tests for the restic_repo_endpoint filter plugin -- the endpoint
parser feeding site.yml's early external-reachability preflight.

Run: uv run pytest tests/unit/test_restic_repo_endpoint.py
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_PLUGIN = (
    Path(__file__).resolve().parents[3]
    / "ansible" / "playbooks" / "filter_plugins" / "restic_repo_endpoint.py"
)
_spec = importlib.util.spec_from_file_location("restic_repo_endpoint", _PLUGIN)
rre = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rre)

f = rre.restic_repo_endpoint


def test_implicit_https_bare_host() -> None:
    assert f("s3:s3.bhs.io.cloud.ovh.net/client-restic") == \
        "https://s3.bhs.io.cloud.ovh.net"


def test_explicit_https() -> None:
    assert f("s3:https://s3.example.com/bucket/sub") == "https://s3.example.com"


def test_explicit_http_with_port_bench_minio() -> None:
    assert f("s3:http://10.139.244.250:9000/catena-testbench-restic") == \
        "http://10.139.244.250:9000"


def test_non_s3_backends_skip() -> None:
    # sftp/rest/local backends are validated by their own transport, not an
    # HTTP probe -- the filter returns "" so the preflight skips them.
    assert f("sftp:user@host:/srv/restic") == ""
    assert f("rest:https://rest.example.com/") == ""
    assert f("/mnt/data/local-restic") == ""


def test_empty_and_non_string() -> None:
    assert f("") == ""
    assert f(None) == ""
    assert f("   ") == ""


def test_endpoint_only_no_bucket() -> None:
    # Defensive: an endpoint with no trailing bucket still yields the host.
    assert f("s3:s3.example.com") == "https://s3.example.com"
    assert f("s3:http://host:9000") == "http://host:9000"
