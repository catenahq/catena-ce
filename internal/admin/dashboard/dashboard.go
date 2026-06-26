// Package dashboard assembles the client-facing proof dashboard: the
// peace-of-mind view that renders, in plain language, the proof artifacts
// the host already writes as JSON (restore drills, backup integrity,
// monitoring, CVE posture, port exposure) plus a live SSO health probe.
// Every field degrades to a "not yet measured" empty-state so a fresh or
// Community-only host renders rather than crashing.
package dashboard

import (
	"net/http"
	"time"

	"github.com/catenahq/catena-ce/internal/admin/integrations"
	"github.com/catenahq/catena-ce/internal/admin/stats"
)

// GatusLister is the monitoring read surface BuildDashboard falls back to
// when the cached homepage-summary.json is absent. The concrete Gatus
// client satisfies it (and tests can fake it).
type GatusLister interface {
	ListStatuses() []integrations.EndpointStatus
}

// ssoProbeTimeout caps the live Keycloak readiness probe so a slow or
// unreachable IdP never stalls the dashboard render.
const ssoProbeTimeout = 1500 * time.Millisecond

// Result is one proof artifact's outcome. OK is nil when no report exists
// yet (the host has not run the producing lane), so the template can show
// "not yet measured" distinctly from pass/fail.
type Result struct {
	OK     *bool
	TS     string
	RTO    string // restore time objective, "" until the emitter captures it
	Detail string
}

// StatusClass is the CSS modifier the template uses for the status dot,
// matching system.InfraEntry: healthy / unhealthy / unknown.
func (r Result) StatusClass() string {
	if r.OK == nil {
		return "unknown"
	}
	if *r.OK {
		return "healthy"
	}
	return "unhealthy"
}

// Dashboard is the proof-dashboard panel data.
type Dashboard struct {
	HotDrill    Result // local restore proven (verify-hot)
	ColdDrill   Result // offsite copy proven (verify-cold)
	BackupCheck Result // repo integrity (restic-check)
	BackupSize  string
	BackupCount int
	BackupLast  string

	SSO Result // live Keycloak readiness probe

	PortsExposed  *int // nil = not yet measured
	PortsExpected int

	CVEReported bool
	CVECritical int
	CVEHigh     int

	MonTotal int
	MonUp    int
	MonDown  int
}

// PortsMeasured reports whether the port-scan emitter has written a result
// yet (distinguishes "0 exposed" from "not yet measured").
func (d Dashboard) PortsMeasured() bool { return d.PortsExposed != nil }

// PortsCount is the measured exposed-port count, or 0 when unmeasured.
func (d Dashboard) PortsCount() int {
	if d.PortsExposed == nil {
		return 0
	}
	return *d.PortsExposed
}

// PortsClass is the status-dot modifier for the ports line: healthy when no
// unexpected port is exposed, unhealthy when any is, unknown when unmeasured.
func (d Dashboard) PortsClass() string {
	if d.PortsExposed == nil {
		return "unknown"
	}
	if *d.PortsExposed == 0 {
		return "healthy"
	}
	return "unhealthy"
}

// BuildDashboard reads the host stat files + a live SSO probe into the
// panel data. gatus may be nil (unconfigured) and keycloakProbeURL may be
// empty (SSO unknown) -- both degrade rather than panicking.
func BuildDashboard(gatus GatusLister, statsDir, keycloakProbeURL string) Dashboard {
	hot := stats.VerifyHot(statsDir)
	cold := stats.VerifyCold(statsDir)
	check := stats.ResticCheck(statsDir)
	backup := stats.Backup(statsDir)
	cve := stats.ContainerCVE(statsDir)
	ports := stats.PortScan(statsDir)

	d := Dashboard{
		HotDrill: Result{
			OK:     exitOK(hot),
			TS:     stats.String(hot, "ts"),
			RTO:    stats.String(hot, "rto"),
			Detail: stats.String(hot, "message"),
		},
		ColdDrill: Result{
			OK:     exitOK(cold),
			TS:     stats.String(cold, "latest_snapshot_time"),
			Detail: stats.String(cold, "message"),
		},
		BackupCheck: Result{
			OK:     exitOK(check),
			TS:     stats.String(check, "last_run"),
			Detail: stats.String(check, "message"),
		},
		BackupSize:  stats.String(backup, "repo_size_human"),
		BackupCount: stats.Int(backup, "snapshot_count"),
		BackupLast:  stats.String(backup, "last_snapshot_at"),
		SSO:         Result{OK: probeHTTP(keycloakProbeURL)},
	}

	if stats.Has(ports, "exposed_count") {
		n := stats.Int(ports, "exposed_count")
		d.PortsExposed = &n
		d.PortsExpected = stats.Int(ports, "expected_count")
	}

	if stats.Has(cve, "exit") {
		d.CVEReported = true
		d.CVECritical = stats.Int(cve, "count_critical")
		d.CVEHigh = stats.Int(cve, "count_high")
	}

	d.MonTotal, d.MonUp, d.MonDown = monitoring(gatus, statsDir)
	return d
}

// exitOK maps a report's "exit" field to a tri-state pass/fail/unknown:
// absent => nil (no report yet), 0 => pass, non-zero => fail.
func exitOK(m map[string]any) *bool {
	if !stats.Has(m, "exit") {
		return nil
	}
	ok := stats.Int(m, "exit") == 0
	return &ok
}

// monitoring prefers the cached homepage-summary.json (no live call); it
// falls back to counting the live Gatus statuses when the cache is absent.
func monitoring(gatus GatusLister, statsDir string) (total, up, down int) {
	summary := stats.HomepageSummary(statsDir)
	if stats.Has(summary, "total") {
		return stats.Int(summary, "total"), stats.Int(summary, "up"), stats.Int(summary, "down")
	}
	if gatus == nil {
		return 0, 0, 0
	}
	for _, s := range gatus.ListStatuses() {
		total++
		if s.Healthy != nil && *s.Healthy {
			up++
		} else if s.Healthy != nil {
			down++
		}
	}
	return total, up, down
}

// probeHTTP GETs url with a short timeout: empty url => nil (SSO surface
// unconfigured / unknown), a 2xx => healthy, any error or non-2xx => down.
func probeHTTP(url string) *bool {
	if url == "" {
		return nil
	}
	client := &http.Client{Timeout: ssoProbeTimeout}
	resp, err := client.Get(url) // #nosec G107 -- url is an operator-set env var, not request input
	if err != nil {
		down := false
		return &down
	}
	defer func() { _ = resp.Body.Close() }()
	ok := resp.StatusCode >= 200 && resp.StatusCode < 300
	return &ok
}
