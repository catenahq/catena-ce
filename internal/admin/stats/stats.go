// Package stats reads JSON stats files the host writes into the catena-admin
// bind-mount (/var/lib/catena), e.g. backup-stats.json from run-backup.sh.
// Until the writers run on a fresh host the files may be absent; every read
// falls back to an empty map so pages render an "unavailable" placeholder
// rather than crashing. No caching -- a file read is sub-millisecond and each
// render re-reads so a refresh after an action shows the new state.
package stats

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
)

// Dir resolves the stats directory: the override, else CATENA_ADMIN_STATS_DIR,
// else /var/lib/catena.
func Dir(override string) string {
	if strings.TrimSpace(override) != "" {
		return override
	}
	if env := strings.TrimSpace(os.Getenv("CATENA_ADMIN_STATS_DIR")); env != "" {
		return env
	}
	return "/var/lib/catena"
}

// Read returns <dir>/<name>.json as a map, or an empty map on any failure
// (missing file, invalid JSON, permission error).
func Read(name, statsDir string) map[string]any {
	// Guard the name to a plain base filename so it can never traverse out of
	// the stats dir, regardless of caller.
	if name == "" || name != filepath.Base(name) {
		return map[string]any{}
	}
	path := filepath.Join(Dir(statsDir), name+".json")
	raw, err := os.ReadFile(path) // #nosec G304 -- name guarded to a base filename above; statsDir is operator-configured
	if err != nil {
		return map[string]any{}
	}
	var out map[string]any
	if err := json.Unmarshal(raw, &out); err != nil || out == nil {
		return map[string]any{}
	}
	return out
}

// Backup reads backup-stats.json (repo_size_human, snapshot_count,
// last_snapshot_at) written by run-backup.sh.
func Backup(statsDir string) map[string]any {
	return Read("backup-stats", statsDir)
}
