"""Topological-sort filter for the pg_replay dependency graph.

Used by `roles/backup/tasks/pg_replay.yml` to order per-database
replays such that dependencies replay before their dependents.

Input shape (a list of dicts):

    [
      {"name": "dokploy-postgres", "depends_on": []},
      {"name": "nextcloud-db",     "depends_on": []},
      {"name": "n8n-db",           "depends_on": ["nextcloud-db"]},
    ]

Output: a list of names ordered such that every dependency precedes
its dependents (Kahn's algorithm). Independent nodes preserve their
input order, then break ties lexically. Cycles -> AnsibleFilterError.

Unknown deps (a node depends on a name not in the input) are silently
ignored: pg_replay only sees basenames whose dumps actually exist in
backup-staging, so a catalog dep on a not-yet-seeded template is a
no-op rather than an error.
"""
from __future__ import annotations

from collections import deque

from ansible.errors import AnsibleFilterError


def topo_sort(graph):
    if not isinstance(graph, list):
        raise AnsibleFilterError(
            f"topo_sort: expected list of dicts, got {type(graph).__name__}"
        )
    nodes = {}
    for entry in graph:
        if not isinstance(entry, dict) or "name" not in entry:
            raise AnsibleFilterError(
                f"topo_sort: each entry must be a dict with 'name'; got {entry!r}"
            )
        name = entry["name"]
        deps = entry.get("depends_on") or []
        if not isinstance(deps, list):
            raise AnsibleFilterError(
                f"topo_sort: 'depends_on' for {name!r} must be a list, got {deps!r}"
            )
        nodes[name] = list(deps)

    indegree = {n: 0 for n in nodes}
    edges = {n: [] for n in nodes}
    for n, deps in nodes.items():
        for d in deps:
            if d not in nodes:
                continue
            edges[d].append(n)
            indegree[n] += 1

    ready = deque(sorted(n for n, c in indegree.items() if c == 0))
    out = []
    while ready:
        n = ready.popleft()
        out.append(n)
        for m in sorted(edges[n]):
            indegree[m] -= 1
            if indegree[m] == 0:
                ready.append(m)

    if len(out) != len(nodes):
        remaining = sorted(n for n, c in indegree.items() if c > 0)
        raise AnsibleFilterError(
            f"topo_sort: cycle detected involving: {remaining}"
        )
    return out


def pg_replay_order(basenames, catalog=None):
    """Order pg_replay basenames by catalog `pg_replay_depends_on`.

    `basenames` is the list of dump-file basenames seen in
    backup-staging/pg. `catalog` is the dokploy_template_catalog list
    (or None / empty list -> falls back to lex-sort).

    Catalog entries map `app_name` -> `pg_replay_depends_on`. We match
    catalog entries to basenames by membership: a catalog entry with
    app_name="nextcloud" applies to a basename "nextcloud" or any
    basename that starts with "nextcloud-" (Dokploy compose names that
    embed the app slug, e.g. "nextcloud-db"). Dependencies are only
    enforced when both endpoints are present in `basenames`; otherwise
    the dep is dropped (a templated app that hasn't been deployed has
    no dump and cannot block anything).
    """
    if not basenames:
        return []
    if not catalog:
        return sorted(basenames)

    deps_by_basename = {b: [] for b in basenames}
    for entry in catalog:
        if not isinstance(entry, dict):
            continue
        app = entry.get("app_name") or entry.get("id")
        if not app:
            continue
        deps = entry.get("pg_replay_depends_on") or []
        if not deps:
            continue
        targets = [b for b in basenames if b == app or b.startswith(f"{app}-")]
        for t in targets:
            for d in deps:
                matches = [b for b in basenames if b == d or b.startswith(f"{d}-")]
                deps_by_basename[t].extend(matches)

    graph = [
        {"name": b, "depends_on": sorted(set(deps))}
        for b, deps in deps_by_basename.items()
    ]
    return topo_sort(graph)


class FilterModule:
    def filters(self):
        return {
            "topo_sort": topo_sort,
            "pg_replay_order": pg_replay_order,
        }
