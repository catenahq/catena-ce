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

// VerifyHot reads verify-hot.json (exit, ts, pg_archives_checked,
// compose_files_checked, bootprobe, rto) written by catena-verify-hot.sh.
func VerifyHot(statsDir string) map[string]any {
	return Read("verify-hot", statsDir)
}

// VerifyCold reads verify-cold.json (exit, ts, latest_snapshot_id,
// latest_snapshot_time) written by catena-verify-cold.sh.
func VerifyCold(statsDir string) map[string]any {
	return Read("verify-cold", statsDir)
}

// ResticCheck reads restic-check.json (exit, last_run, last_subset_run)
// written by catena-restic-check.sh.
func ResticCheck(statsDir string) map[string]any {
	return Read("restic-check", statsDir)
}

// ContainerCVE reads container-cve-findings.json (exit, count_critical,
// count_high, scanned_images) written by catena-container-cve-scan.sh.
func ContainerCVE(statsDir string) map[string]any {
	return Read("container-cve-findings", statsDir)
}

// HomepageSummary reads homepage-summary.json (total, up, down, down_names)
// written by gatus-sync.py.
func HomepageSummary(statsDir string) map[string]any {
	return Read("homepage-summary", statsDir)
}

// PortScan reads port-scan.json (ts, exposed_count, unexpected, expected)
// written by the external port-scan emitter.
func PortScan(statsDir string) map[string]any {
	return Read("port-scan", statsDir)
}

// String returns m[k] as a string, or "" if absent or not a string.
func String(m map[string]any, k string) string {
	if v, ok := m[k].(string); ok {
		return v
	}
	return ""
}

// Int returns m[k] as an int, coercing the float64 JSON numbers decode to.
// Absent or non-numeric yields 0.
func Int(m map[string]any, k string) int {
	switch n := m[k].(type) {
	case float64:
		return int(n)
	case int:
		return n
	}
	return 0
}

// Has reports whether m carries key k with a non-nil value. Lets callers
// tell "field absent" (no report yet) from "field present and zero".
func Has(m map[string]any, k string) bool {
	v, ok := m[k]
	return ok && v != nil
}
