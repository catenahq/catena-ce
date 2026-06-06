// Package maintenance assembles the Maintenance-log tab, ported from the
// Python catena_admin.maintenance. It reads the client-readable maintenance
// feed the host-side catena-maintenance-log helper appends to and localizes
// each STRUCTURED row (never pre-rendered prose) into a one-line entry.
package maintenance

import (
	"encoding/json"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"strconv"
	"strings"

	"github.com/catenahq/catena-ce/internal/admin/i18n"
)

const (
	defaultLimit     = 50
	logFileEnv       = "CATENA_ADMIN_MAINTENANCE_LOG_FILE"
	statsDirEnv      = "CATENA_ADMIN_STATS_DIR"
	statsDirFallback = "/var/lib/catena"
)

// Entry is one localized maintenance line for the request's locale.
type Entry struct {
	TS       string
	Severity string // "success" | "info" | "warn"
	Message  string
}

type row struct {
	TS       string         `json:"ts"`
	Severity string         `json:"severity"`
	Code     string         `json:"code"`
	Params   map[string]any `json:"params"`
}

func logPath(statsDir string) string {
	if v := strings.TrimSpace(os.Getenv(logFileEnv)); v != "" {
		return v
	}
	base := statsDir
	if base == "" {
		if v := strings.TrimSpace(os.Getenv(statsDirEnv)); v != "" {
			base = v
		} else {
			base = statsDirFallback
		}
	}
	return filepath.Join(base, "maintenance-log.json")
}

// readLog reads the JSON array. Any failure (missing file, invalid JSON,
// non-array) yields nil so a fresh host renders the empty state.
func readLog(statsDir string) []row {
	raw, err := os.ReadFile(logPath(statsDir))
	if err != nil {
		return nil
	}
	var rows []row
	if err := json.Unmarshal(raw, &rows); err != nil {
		return nil
	}
	return rows
}

// BuildEntries localizes the last `limit` rows newest-first. Each row's message
// is maintenance.event.<code> interpolated with its params; an unknown or empty
// code renders the generic _unknown line so a writer that emits a code before
// its translation lands never leaks a raw code.
func BuildEntries(tr *i18n.Translations, locale, statsDir string, limit int) []Entry {
	if limit <= 0 {
		limit = defaultLimit
	}
	known := make(map[string]bool)
	for _, k := range tr.Keys("en") {
		known[k] = true
	}
	rows := readLog(statsDir)
	if len(rows) > limit {
		rows = rows[len(rows)-limit:]
	}
	out := make([]Entry, 0, len(rows))
	for i := len(rows) - 1; i >= 0; i-- { // newest-first
		r := rows[i]
		key := "maintenance.event." + r.Code
		if r.Code == "" || !known[key] {
			key = "maintenance.event._unknown"
		}
		sev := r.Severity
		if sev == "" {
			sev = "info"
		}
		out = append(out, Entry{
			TS:       r.TS,
			Severity: sev,
			Message:  tr.Get(key, locale, stringifyParams(r.Params)),
		})
	}
	return out
}

func stringifyParams(in map[string]any) map[string]string {
	if len(in) == 0 {
		return nil
	}
	out := make(map[string]string, len(in))
	for k, v := range in {
		out[k] = stringifyParam(v)
	}
	return out
}

func stringifyParam(v any) string {
	switch t := v.(type) {
	case string:
		return t
	case float64: // JSON numbers decode to float64
		if t == math.Trunc(t) {
			return strconv.FormatInt(int64(t), 10)
		}
		return strconv.FormatFloat(t, 'f', -1, 64)
	case bool:
		return strconv.FormatBool(t)
	case nil:
		return ""
	default:
		return fmt.Sprintf("%v", t)
	}
}
