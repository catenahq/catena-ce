// Package system assembles the System-tab snapshot from the Gatus +
// Healthchecks clients and the host backup-stats. Admin-only -- the route
// applies the gate upstream. Ported from the Python catena_admin.system.
package system

import (
	"strings"

	"github.com/catenahq/catena-ce/internal/admin/integrations"
	"github.com/catenahq/catena-ce/internal/admin/stats"
)

// infraEndpointKeys are the Gatus infrastructure-group endpoints the System
// tab pins, cross-referenced with their probe state.
var infraEndpointKeys = []string{
	"dokploy", "dokploy-traefik", "gatus", "healthchecks", "keycloak", "oauth2-proxy",
}

// GatusLister + HCLister are the read surfaces BuildSnapshot needs; the
// concrete integration clients satisfy them (and tests can fake them).
type GatusLister interface {
	ListStatuses() []integrations.EndpointStatus
}

type HCLister interface {
	ListChecks() []integrations.Check
}

// Alert is one currently-unhealthy surface (a down Gatus endpoint, or a
// down/late Healthchecks check).
type Alert struct {
	Source   string // gatus | healthchecks | managed-update
	Target   string
	Severity string // down | late | quarantined
	Detail   string
}

// InfraEntry is one pinned infrastructure surface with its probe state.
// Healthy is nil when no probe matched.
type InfraEntry struct {
	Name    string
	Healthy *bool
	Detail  string
}

// StatusClass is the CSS modifier the template uses for the status dot:
// healthy / unhealthy / unknown (nil probe).
func (e InfraEntry) StatusClass() string {
	if e.Healthy == nil {
		return "unknown"
	}
	if *e.Healthy {
		return "healthy"
	}
	return "unhealthy"
}

// Snapshot is the System-tab panel data.
type Snapshot struct {
	BackupSize  string
	BackupCount int
	BackupLast  string
	DiskUsedPct *int
	DiskDetail  string
	HCTotal     int
	HCUp        int
	HCLate      int
	HCDown      int
	Alerts      []Alert
	Infra       []InfraEntry
}

// BuildSnapshot reads the clients + host backup/disk stats into the panel
// data. A nil client (unconfigured surface) contributes nothing rather than
// panicking, so the page still renders on a partly-configured host.
func BuildSnapshot(gatus GatusLister, hc HCLister, statsDir string) Snapshot {
	var statuses []integrations.EndpointStatus
	if gatus != nil {
		statuses = gatus.ListStatuses()
	}
	var checks []integrations.Check
	if hc != nil {
		checks = hc.ListChecks()
	}
	backup := stats.Backup(statsDir)
	disk := stats.Read("disk", statsDir)

	total, up, late, down := hcCounts(checks)
	return Snapshot{
		BackupSize:  getString(backup, "repo_size_human"),
		BackupCount: getInt(backup, "snapshot_count"),
		BackupLast:  getString(backup, "last_snapshot_at"),
		DiskUsedPct: diskPct(disk),
		DiskDetail:  getString(disk, "detail"),
		HCTotal:     total,
		HCUp:        up,
		HCLate:      late,
		HCDown:      down,
		Alerts:      assembleAlerts(statuses, checks),
		Infra:       assembleInfra(statuses),
	}
}

func hcCounts(checks []integrations.Check) (total, up, late, down int) {
	for _, c := range checks {
		total++
		switch c.Status {
		case "up":
			up++
		case "late":
			late++
		case "down":
			down++
		}
	}
	return
}

func diskPct(disk map[string]any) *int {
	v, ok := disk["used_pct"]
	if !ok || v == nil {
		return nil
	}
	switch n := v.(type) {
	case float64:
		i := int(n)
		return &i
	case int:
		return &n
	}
	return nil
}

func assembleAlerts(statuses []integrations.EndpointStatus, checks []integrations.Check) []Alert {
	var out []Alert
	for _, s := range statuses {
		if s.Healthy != nil && !*s.Healthy {
			out = append(out, Alert{Source: "gatus", Target: s.Name, Severity: "down", Detail: s.URL})
		}
	}
	for _, c := range checks {
		if c.Status == "down" || c.Status == "late" {
			detail := c.LastPing
			if detail == "" {
				detail = "(never pinged)"
			}
			out = append(out, Alert{Source: "healthchecks", Target: c.Name, Severity: c.Status, Detail: detail})
		}
	}
	return out
}

func assembleInfra(statuses []integrations.EndpointStatus) []InfraEntry {
	byName := make(map[string]integrations.EndpointStatus, len(statuses))
	for _, s := range statuses {
		byName[strings.ToLower(s.Name)] = s
	}
	out := make([]InfraEntry, 0, len(infraEndpointKeys))
	for _, key := range infraEndpointKeys {
		s, ok := byName[key]
		if !ok {
			// Substring match for e.g. "gatus-internal" vs "gatus".
			for n, candidate := range byName {
				if strings.Contains(n, key) {
					s, ok = candidate, true
					break
				}
			}
		}
		if !ok {
			out = append(out, InfraEntry{Name: key, Healthy: nil, Detail: "(no probe)"})
		} else {
			out = append(out, InfraEntry{Name: key, Healthy: s.Healthy, Detail: s.Name})
		}
	}
	return out
}

func getString(m map[string]any, k string) string {
	if v, ok := m[k].(string); ok {
		return v
	}
	return ""
}

func getInt(m map[string]any, k string) int {
	switch n := m[k].(type) {
	case float64:
		return int(n)
	case int:
		return n
	}
	return 0
}
