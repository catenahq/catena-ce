#!/bin/sh
# Installed by roles/backup. Renders an HTML index of restic snapshots
# currently present in the repo into ${BACKUP_EXPORT_DIR}/index.html.
# That dir is served read-only by the recovery-downloads nginx sidecar
# at recovery.<zone>; nginx serves index.html before falling through to
# autoindex, so client admins land on a curated snapshot listing instead
# of a raw directory dump.
#
# F6 (BACKLOG): replaces the older "weekly off-site link" framing. Page
# is regenerated:
#   - after every backup run (run-backup.sh, post-prune)
#   - after every snapshot-export (snapshot-export.sh)
#   - on demand by the operator: sudo /usr/local/bin/catena-snapshot-list
#
# All per-host values come from the env file at $1 (or
# /etc/catena/backup.env by default). This script is plain /bin/sh
# with NO Jinja markup -- deployed via ansible.builtin.copy.
#
# Exit codes: 0 on success, non-zero if BACKUP_EXPORT_DIR is unset
# or unwritable. Restic-snapshots failures degrade gracefully (page
# renders with the empty-list message).

set -eu

ENV_FILE="${1:-/etc/catena/backup.env}"
# Same set -a discipline as run-backup.sh; see
# tests/unit/test_restic_env_pattern.py for the rationale.
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

if [ -z "${BACKUP_EXPORT_DIR:-}" ]; then
    printf 'snapshot-list: BACKUP_EXPORT_DIR not set\n' >&2
    exit 2
fi
mkdir -p "${BACKUP_EXPORT_DIR}"

INDEX_HTML="${BACKUP_EXPORT_DIR}/index.html"
SNAPSHOTS_FILE="$(mktemp)"
trap 'rm -f "${SNAPSHOTS_FILE}"' EXIT

restic snapshots --json > "${SNAPSHOTS_FILE}" 2>/dev/null || echo '[]' > "${SNAPSHOTS_FILE}"

GENERATED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
ZONE="${CLOUDFLARE_ZONE:-YOUR-DOMAIN}"

python3 - "${INDEX_HTML}" "${BACKUP_EXPORT_DIR}" "${GENERATED_AT}" "${ZONE}" "${SNAPSHOTS_FILE}" <<'PY'
import json
import os
import sys
from html import escape

index_html, export_dir, generated_at, zone, snapshots_file = sys.argv[1:6]

with open(snapshots_file, "r", encoding="utf-8") as fh:
    try:
        snapshots = json.load(fh)
    except json.JSONDecodeError:
        snapshots = []

snapshots.sort(key=lambda s: s.get("time", ""), reverse=True)

# List pre-exported tarballs sitting in BACKUP_EXPORT_DIR alongside
# index.html. The "Export latest snapshot" OliveTin button writes one
# at a time and rotates; older runs may leave additional files behind.
exported = []
for name in sorted(os.listdir(export_dir)):
    if name == "index.html":
        continue
    full = os.path.join(export_dir, name)
    try:
        size = os.path.getsize(full)
    except OSError:
        continue
    if size <= 0:
        continue
    exported.append((name, size))


def fmt_size(b):
    for unit, threshold in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if b >= threshold:
            return f"{b / threshold:.2f} {unit}"
    return f"{b} B"


snapshot_rows = []
for snap in snapshots:
    sid = snap.get("short_id") or snap.get("id", "")[:8]
    when = snap.get("time", "")[:19].replace("T", " ")
    host = snap.get("hostname", "")
    paths = ", ".join(snap.get("paths", []))
    tags = ", ".join(snap.get("tags") or [])
    snapshot_rows.append(
        "        <tr>"
        f"<td><code>{escape(sid)}</code></td>"
        f"<td>{escape(when)} UTC</td>"
        f"<td>{escape(host)}</td>"
        f"<td>{escape(paths)}</td>"
        f"<td>{escape(tags)}</td>"
        "</tr>"
    )

export_rows = []
for name, size in exported:
    export_rows.append(
        "        <tr>"
        f'<td><a href="{escape(name)}">{escape(name)}</a></td>'
        f"<td>{escape(fmt_size(size))}</td>"
        "</tr>"
    )

snapshot_table = "\n".join(snapshot_rows) or (
    '        <tr><td colspan="5"><em>No snapshots in repo. Run a backup first.</em></td></tr>'
)
export_table = "\n".join(export_rows) or (
    '        <tr><td colspan="2"><em>No tarballs exported yet. Trigger "Export latest snapshot" '
    "from the actions dashboard to create one.</em></td></tr>"
)

html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Snapshots &amp; exports -- recovery downloads</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         max-width: 980px; margin: 2rem auto; padding: 0 1rem; color: #222; }}
  h1, h2 {{ font-weight: 600; }}
  table {{ border-collapse: collapse; width: 100%; margin-bottom: 2rem; }}
  th, td {{ text-align: left; padding: 0.4rem 0.6rem; border-bottom: 1px solid #ddd; }}
  th {{ background: #f6f6f6; }}
  code {{ background: #f4f4f4; padding: 0 0.3rem; border-radius: 3px; }}
  .meta {{ color: #666; font-size: 0.9rem; }}
  .note {{ background: #fff8dc; border-left: 4px solid #e2c44d; padding: 0.6rem 1rem; }}
</style>
</head>
<body>
<h1>Snapshots &amp; exports</h1>
<p class="meta">Page regenerated <code>{escape(generated_at)}</code>.
Re-rendered after every backup run, after every snapshot export, and after restic
forget+prune. The list below reflects the live state of the restic repository.</p>

<h2>Restic snapshots in repository</h2>
<p>These are the recovery points currently retained. Older snapshots are pruned
by the retention policy (<code>BACKUP_KEEP_DAILY</code> /
<code>BACKUP_KEEP_WEEKLY</code> / <code>BACKUP_KEEP_MONTHLY</code> in the operator
configuration).</p>
<table>
  <thead><tr><th>Snapshot</th><th>Time</th><th>Host</th><th>Paths</th><th>Tags</th></tr></thead>
  <tbody>
{snapshot_table}
  </tbody>
</table>

<h2>Pre-exported tarballs</h2>
<p>Each <code>.tar.gz</code> below is a self-contained extract of a snapshot
written by the &ldquo;Export latest snapshot&rdquo; action. Click to download.</p>
<table>
  <thead><tr><th>File</th><th>Size</th></tr></thead>
  <tbody>
{export_table}
  </tbody>
</table>

<div class="note">
<strong>Need a download for a specific snapshot?</strong> Open
<a href="https://actions.{escape(zone)}/">your actions dashboard</a> and run
<em>Export latest snapshot</em>. To export a non-latest snapshot, ask your
operator (the export action accepts a snapshot ID via the runbook).
</div>

</body>
</html>
"""

with open(index_html, "w", encoding="utf-8") as fh:
    fh.write(html)

os.chmod(index_html, 0o644)
PY

printf 'snapshot-list: wrote %s (%d bytes)\n' \
    "${INDEX_HTML}" "$(stat -c%s "${INDEX_HTML}")"
