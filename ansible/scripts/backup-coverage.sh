#!/bin/bash
# Enumerate every bind-mount source used by running containers, flag any
# path that ISN'T under a covered prefix. Warn-only -- never exits
# non-zero; the backup wrapper calls this as a reporting tail.
#
# Installed by roles/backup (ansible.builtin.copy, NOT .template) --
# keeping it out of Jinja avoids the bash-vs-Jinja fights this file
# suffered as a .j2: ${#var} reads as a jinja comment opener,
# `docker --format '{{.Field}}'` reads as a Jinja expression.
#
# Data inputs come from a plain text file at COVERAGE_PATHS_FILE
# (default /etc/catena/backup-coverage.paths), one path per line,
# blank lines and `#` comments ignored. That file IS templated by the
# role so it stays in sync with backup_paths -- but it's pure data, no
# code, so it can't collide with Jinja tokenizer rules.

set -o pipefail
# Intentionally NOT `set -u`: associative arrays that were declared but
# never assigned trip `${#arr[@]}` under nounset on some bash versions,
# and this script's "happy path" (zero uncovered sources) is exactly
# that case.

COVERAGE_PATHS_FILE="${COVERAGE_PATHS_FILE:-/etc/catena/backup-coverage.paths}"

if [ ! -r "$COVERAGE_PATHS_FILE" ]; then
    echo "backup-coverage: missing or unreadable $COVERAGE_PATHS_FILE -- was the backup role applied?" >&2
    exit 0
fi

# Read one path per line, skipping blanks and comments.
COVERED_PREFIXES=()
while IFS= read -r line; do
    line="${line%%#*}"
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    [ -z "$line" ] && continue
    COVERED_PREFIXES+=("$line")
done < "$COVERAGE_PATHS_FILE"

# Prefix-match ignores -- paths whose entire subtree is kernel/runtime
# interface surface, not persistent data. Trailing slashes are NOT
# included here: the match rule below is `$ig OR $ig/*`, which covers
# both the bare path (e.g. "/sys") and anything under it ("/sys/block").
# Prior bug: trailing slashes here caused a Homepage container mounting
# "/proc" and "/sys" (bare paths, no slash) to be flagged as uncovered.
IGNORE_PREFIXES=(
    "/var/run/docker.sock"
    "/var/run"
    "/proc"
    "/sys"
    "/dev"
    "/var/lib/catena"
    # Recovery-bundle + snapshot-export staging dir. Bind-mounted
    # read-only into the recovery-downloads nginx sidecar. Intentionally
    # NOT in backup_paths -- including it would be self-referential: the
    # files are already a packaged form of the backup. Suppress the
    # "uncovered bind mount" warning for this mount so the nightly
    # coverage report doesn't cry wolf.
    "/var/backups/catena-export"
)

# Exact-match ignores -- the bind source is LITERALLY this path, and
# prefix-matching would mask legitimate data paths underneath. E.g.
# adding /mnt/data as a prefix-ignore would silently stop flagging any
# uncovered subdir under it.
#
#   /mnt/data                -- Homepage's disk-usage widget bind-mounts
#                              the storage mount-point read-only so it
#                              can compute totals. Monitoring surface,
#                              not persistent data.
#   /mnt/data/vps-docs/site  -- Per-client docs wiki, re-synced from the
#                              operator-side mkdocs build on every
#                              converge (see roles/infrastructure/
#                              tasks/vps_docs.yml). Regenerable; not
#                              authoritative state.
IGNORE_EXACT=(
    "/mnt/data"
    "/mnt/data/vps-docs/site"
)

echo "=== Backup coverage check ($(date -u +%Y-%m-%dT%H:%M:%SZ)) ==="
echo "Covered prefixes (backup_paths):"
for p in "${COVERED_PREFIXES[@]}"; do
    echo "  - $p"
done
echo

# Collect bind-mount sources across ALL running containers. Swarm tasks
# + compose containers both show up in `docker ps`. Named volumes have
# Type=volume; we skip those (already covered). Bind mounts are the risk.
mapfile -t rows < <(
    docker ps --format '{{.Names}}' 2>/dev/null | while read -r ctr; do
        [ -z "$ctr" ] && continue
        docker inspect "$ctr" \
            --format '{{range .Mounts}}{{.Type}}|{{.Source}}|'"$ctr"$'\n''{{end}}' \
            2>/dev/null
    done | sort -u | grep -v '^$'
)

declare -A uncovered_sources
covered_count=0
ignored_count=0

for row in "${rows[@]}"; do
    type="${row%%|*}"
    rest="${row#*|}"
    src="${rest%%|*}"
    ctr="${rest##*|}"

    # Volumes are auto-covered; skip.
    if [ "$type" != "bind" ]; then
        continue
    fi
    [ -z "$src" ] && continue

    # Ignore system / interface paths + known regenerable mounts.
    ignored=0
    for ig in "${IGNORE_PREFIXES[@]}"; do
        case "$src" in
            "$ig"|"$ig"/*) ignored=1; break;;
        esac
    done
    if [ "$ignored" = 0 ]; then
        for ig in "${IGNORE_EXACT[@]}"; do
            if [ "$src" = "$ig" ]; then
                ignored=1
                break
            fi
        done
    fi
    if [ "$ignored" = 1 ]; then
        ignored_count=$((ignored_count + 1))
        continue
    fi

    # Does src live under a covered prefix?
    covered=0
    for p in "${COVERED_PREFIXES[@]}"; do
        case "$src" in
            "$p"|"$p/"*) covered=1; break;;
        esac
    done

    if [ "$covered" = 1 ]; then
        covered_count=$((covered_count + 1))
    else
        if [ -n "${uncovered_sources[$src]:-}" ]; then
            uncovered_sources[$src]="${uncovered_sources[$src]}, $ctr"
        else
            uncovered_sources[$src]="$ctr"
        fi
    fi
done

echo "Summary:"
echo "  - bind mounts covered:  $covered_count"
echo "  - bind mounts ignored:  $ignored_count (system / interface paths)"
echo "  - bind mounts UNCOVERED: ${#uncovered_sources[@]}"

if [ "${#uncovered_sources[@]}" -eq 0 ]; then
    echo
    echo "✓ Every application bind-mount source is under a backed-up prefix."
    exit 0
fi

echo
echo "⚠  UNCOVERED bind-mount sources -- these are NOT in the restic backup:"
echo
for src in "${!uncovered_sources[@]}"; do
    printf '    %s\n        used by: %s\n' "$src" "${uncovered_sources[$src]}"
done
echo
echo "To fix, either:"
echo "  a) Move the data under a covered prefix (/mnt/data/apps/<project>/ is"
echo "     the usual spot for client app state), OR"
echo "  b) Add the path to backup_paths in roles/backup/defaults/main.yml"
echo "     (or via inventory override) and re-converge."
echo
echo "Named volumes are always covered (under /mnt/data/docker/volumes)."
echo "Relative bind mounts in Dokploy compose files (e.g. ./myapp) resolve"
echo "under /etc/dokploy/compose/<project>/code/ which is covered too."

exit 0
