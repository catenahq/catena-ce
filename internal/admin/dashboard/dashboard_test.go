package dashboard

import (
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"

	"github.com/catenahq/catena-ce/internal/admin/integrations"
)

type fakeGatus struct{ s []integrations.EndpointStatus }

func (f fakeGatus) ListStatuses() []integrations.EndpointStatus { return f.s }

func boolp(b bool) *bool { return &b }

func write(t *testing.T, dir, name, body string) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(dir, name), []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestBuildDashboardPopulated(t *testing.T) {
	dir := t.TempDir()
	write(t, dir, "verify-hot.json", `{"ts":"2026-06-25T03:00:00Z","exit":0,"message":"ok","rto":"42s"}`)
	write(t, dir, "verify-cold.json", `{"ts":"2026-06-25T03:05:00Z","exit":0,"latest_snapshot_time":"2026-06-25T01:00:00Z","message":"ok"}`)
	write(t, dir, "restic-check.json", `{"exit":0,"last_run":"2026-06-25T03:10:00Z","message":"ok"}`)
	write(t, dir, "backup-stats.json", `{"repo_size_human":"12.3 GB","snapshot_count":47,"last_snapshot_at":"2026-06-25T03:00:00Z"}`)
	write(t, dir, "container-cve-findings.json", `{"ts":"x","exit":0,"count_critical":1,"count_high":4}`)
	write(t, dir, "port-scan.json", `{"ts":"x","exposed_count":0,"expected_count":2}`)
	write(t, dir, "homepage-summary.json", `{"total":10,"up":9,"down":1}`)

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {}))
	defer srv.Close()

	d := BuildDashboard(nil, dir, srv.URL)

	if d.HotDrill.StatusClass() != "healthy" || d.HotDrill.RTO != "42s" {
		t.Errorf("hot drill = %s/%q, want healthy/42s", d.HotDrill.StatusClass(), d.HotDrill.RTO)
	}
	if d.ColdDrill.StatusClass() != "healthy" || d.ColdDrill.TS != "2026-06-25T01:00:00Z" {
		t.Errorf("cold drill = %s/%q", d.ColdDrill.StatusClass(), d.ColdDrill.TS)
	}
	if d.BackupCheck.StatusClass() != "healthy" {
		t.Errorf("backup check = %s, want healthy", d.BackupCheck.StatusClass())
	}
	if d.BackupSize != "12.3 GB" || d.BackupCount != 47 {
		t.Errorf("backup = %q/%d", d.BackupSize, d.BackupCount)
	}
	if !d.CVEReported || d.CVECritical != 1 || d.CVEHigh != 4 {
		t.Errorf("cve = %v/%d/%d", d.CVEReported, d.CVECritical, d.CVEHigh)
	}
	if !d.PortsMeasured() || d.PortsCount() != 0 || d.PortsClass() != "healthy" {
		t.Errorf("ports = measured %v count %d class %s", d.PortsMeasured(), d.PortsCount(), d.PortsClass())
	}
	if d.MonTotal != 10 || d.MonUp != 9 || d.MonDown != 1 {
		t.Errorf("monitoring = %d/%d/%d", d.MonUp, d.MonDown, d.MonTotal)
	}
	if d.SSO.StatusClass() != "healthy" {
		t.Errorf("sso = %s, want healthy (probe 200)", d.SSO.StatusClass())
	}
}

func TestBuildDashboardEmpty(t *testing.T) {
	// Fresh / Community-only host: no stat files, no SSO URL. Everything
	// degrades to the unknown/unmeasured empty-state without panicking.
	d := BuildDashboard(nil, t.TempDir(), "")
	if d.HotDrill.StatusClass() != "unknown" || d.ColdDrill.StatusClass() != "unknown" {
		t.Error("drills should be unknown on a fresh host")
	}
	if d.BackupCheck.StatusClass() != "unknown" || d.SSO.StatusClass() != "unknown" {
		t.Error("backup check + sso should be unknown on a fresh host")
	}
	if d.CVEReported {
		t.Error("cve should be unreported on a fresh host")
	}
	if d.PortsMeasured() || d.PortsClass() != "unknown" {
		t.Error("ports should be unmeasured on a fresh host")
	}
	if d.MonTotal != 0 {
		t.Errorf("monitoring total = %d, want 0", d.MonTotal)
	}
}

func TestExitNonZeroIsUnhealthy(t *testing.T) {
	dir := t.TempDir()
	write(t, dir, "verify-hot.json", `{"ts":"x","exit":1,"message":"restic restore latest failed"}`)
	d := BuildDashboard(nil, dir, "")
	if d.HotDrill.StatusClass() != "unhealthy" {
		t.Errorf("exit=1 hot drill = %s, want unhealthy", d.HotDrill.StatusClass())
	}
}

func TestSSOProbeDownOnError(t *testing.T) {
	// A non-empty URL that refuses the connection reads as down, not unknown.
	d := BuildDashboard(nil, t.TempDir(), "http://127.0.0.1:0/health/ready")
	if d.SSO.StatusClass() != "unhealthy" {
		t.Errorf("unreachable sso = %s, want unhealthy", d.SSO.StatusClass())
	}
}

func TestMonitoringGatusFallback(t *testing.T) {
	// No homepage-summary.json: fall back to counting live Gatus statuses.
	gatus := fakeGatus{s: []integrations.EndpointStatus{
		{Name: "a", Healthy: boolp(true)},
		{Name: "b", Healthy: boolp(false)},
		{Name: "c", Healthy: boolp(true)},
	}}
	d := BuildDashboard(gatus, t.TempDir(), "")
	if d.MonTotal != 3 || d.MonUp != 2 || d.MonDown != 1 {
		t.Errorf("gatus fallback = up %d down %d total %d, want 2/1/3", d.MonUp, d.MonDown, d.MonTotal)
	}
}
