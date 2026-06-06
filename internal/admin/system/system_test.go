package system

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/catenahq/catena-ce/internal/admin/integrations"
)

type fakeGatus struct{ s []integrations.EndpointStatus }

func (f fakeGatus) ListStatuses() []integrations.EndpointStatus { return f.s }

type fakeHC struct{ c []integrations.Check }

func (f fakeHC) ListChecks() []integrations.Check { return f.c }

func boolp(b bool) *bool { return &b }

func writeStats(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "backup-stats.json"),
		[]byte(`{"repo_size_human":"12.3 GB","snapshot_count":47,"last_snapshot_at":"2026-05-15T03:00:00Z"}`), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "disk.json"),
		[]byte(`{"used_pct":42,"detail":"/dev/sda1 12G/100G"}`), 0o644); err != nil {
		t.Fatal(err)
	}
	return dir
}

func TestBuildSnapshot(t *testing.T) {
	gatus := fakeGatus{s: []integrations.EndpointStatus{
		{Name: "dokploy", Healthy: boolp(true), URL: "https://dok/"},
		{Name: "gatus-internal", Healthy: boolp(true), URL: "https://g/"},
		{Name: "nextcloud", Healthy: boolp(false), URL: "https://cloud/"},
	}}
	hc := fakeHC{c: []integrations.Check{
		{Name: "daily", Status: "up"},
		{Name: "mirror", Status: "down", LastPing: ""},
		{Name: "cve", Status: "late", LastPing: "2026-06-05T01:00:00Z"},
	}}

	snap := BuildSnapshot(gatus, hc, writeStats(t))

	if snap.BackupSize != "12.3 GB" || snap.BackupCount != 47 {
		t.Errorf("backup = %q/%d, want 12.3 GB/47", snap.BackupSize, snap.BackupCount)
	}
	if snap.DiskUsedPct == nil || *snap.DiskUsedPct != 42 {
		t.Errorf("disk pct = %v, want 42", snap.DiskUsedPct)
	}
	if snap.HCTotal != 3 || snap.HCUp != 1 || snap.HCLate != 1 || snap.HCDown != 1 {
		t.Errorf("hc counts = total %d up %d late %d down %d", snap.HCTotal, snap.HCUp, snap.HCLate, snap.HCDown)
	}
	// Alerts: the down Gatus endpoint + the down + late HC checks = 3.
	if len(snap.Alerts) != 3 {
		t.Fatalf("alerts = %d, want 3: %+v", len(snap.Alerts), snap.Alerts)
	}
	// "never pinged" placeholder for the empty last_ping.
	var sawNever bool
	for _, a := range snap.Alerts {
		if a.Target == "mirror" && a.Detail == "(never pinged)" {
			sawNever = true
		}
	}
	if !sawNever {
		t.Error("expected the empty-last-ping alert to show (never pinged)")
	}

	// Infra rollup: dokploy matched (healthy), gatus matched via substring
	// (gatus-internal), healthchecks/keycloak/oauth2-proxy/dokploy-traefik
	// have no probe.
	infra := map[string]InfraEntry{}
	for _, e := range snap.Infra {
		infra[e.Name] = e
	}
	if infra["dokploy"].StatusClass() != "healthy" {
		t.Errorf("dokploy infra = %s, want healthy", infra["dokploy"].StatusClass())
	}
	if infra["gatus"].StatusClass() != "healthy" || infra["gatus"].Detail != "gatus-internal" {
		t.Errorf("gatus infra = %s/%q, want healthy/gatus-internal", infra["gatus"].StatusClass(), infra["gatus"].Detail)
	}
	if infra["keycloak"].StatusClass() != "unknown" || infra["keycloak"].Detail != "(no probe)" {
		t.Errorf("keycloak infra = %s/%q, want unknown/(no probe)", infra["keycloak"].StatusClass(), infra["keycloak"].Detail)
	}
}

func TestBuildSnapshotNilClients(t *testing.T) {
	// Unconfigured host: nil clients must not panic; everything degrades.
	snap := BuildSnapshot(nil, nil, t.TempDir())
	if snap.HCTotal != 0 || len(snap.Alerts) != 0 {
		t.Errorf("nil clients should yield empty hc/alerts, got %d/%d", snap.HCTotal, len(snap.Alerts))
	}
	if len(snap.Infra) != len(infraEndpointKeys) {
		t.Errorf("infra rollup should still render all keys, got %d", len(snap.Infra))
	}
}
