"""Ansible filter: derive the HTTP(S) endpoint URL to probe from a restic
repo string, for the early external-service reachability preflight.

The installer must fail FAST when a configured external backend is
unreachable -- not 10-30 minutes into the converge when the backup role
finally tries to write (a restic-repo-unreachable converge once took 29
minutes to surface). `pre_tasks` in site.yml probe the endpoint returned
here with a short timeout before any heavy role runs.

restic S3 repo grammar (the only backend we network-probe here):
    s3:s3.bhs.io.cloud.ovh.net/bucket        -> https://s3.bhs.io.cloud.ovh.net
    s3:https://s3.example.com/bucket          -> https://s3.example.com
    s3:http://10.139.244.250:9000/bucket      -> http://10.139.244.250:9000
An implicit scheme defaults to https (restic's own S3 default). Any
non-S3 repo (sftp:, rest:, a local path, or empty) returns "" so the
caller skips the probe -- those backends are validated by their own
transport, not an HTTP HEAD.
"""
from __future__ import annotations


def restic_repo_endpoint(repo) -> str:
    """The scheme://host[:port] to HTTP-probe for an S3 restic repo, or ""
    when `repo` is empty or a non-S3 backend."""
    if not repo or not isinstance(repo, str):
        return ""
    r = repo.strip()
    if not r.startswith("s3:"):
        return ""
    rest = r[3:]
    if rest.startswith(("http://", "https://")):
        scheme, _, tail = rest.partition("://")
        netloc = tail.split("/", 1)[0]
        return f"{scheme}://{netloc}" if netloc else ""
    # No explicit scheme: restic defaults S3 endpoints to https.
    host = rest.split("/", 1)[0]
    return f"https://{host}" if host else ""


class FilterModule:
    def filters(self):
        return {"restic_repo_endpoint": restic_repo_endpoint}
